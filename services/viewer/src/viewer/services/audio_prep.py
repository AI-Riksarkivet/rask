"""Model-free audio preparation for the read plane — ffmpeg transcode, WAV load, and the math.

NO INFERENCE LIVES HERE, and that is the boundary rather than a description. `runners/voiceprint`
seals the speaker encoder (WeSpeaker via pyannote); this module holds only what is model-free, which
`packages/ratch/modalities/av/wav.py` defines as "an external transcoder, no inference". Its note also
explains why the duplication with ratch is deliberate: "the backend never imports the pipeline
package."

It was `wespeaker.py` and it carried a `VoiceEncoder` that loaded pyannote's WeSpeaker in-process —
a named model for one modality inside the service every modality's reads go through, and a second
copy of the encoder the runner already owned. That class moved to the runner's Ray Serve deployment;
the transcode and the math stayed, because they are media preparation and arithmetic, not a workload.
"""

from __future__ import annotations

import logging
import subprocess
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from pydantic import BaseModel


logger = logging.getLogger(__name__)

#: Sample rate / channel count pyannote's models expect.
TARGET_SAMPLE_RATE: int = 16_000

#: HF model id whose "embedding" subfolder holds the WeSpeaker-ResNet34 encoder.
DEFAULT_MODEL: str = "pyannote/speaker-diarization-community-1"

#: Subfolder of :data:`DEFAULT_MODEL` the standalone encoder is loaded from.
EMBEDDING_SUBFOLDER: str = "embedding"

#: Dimensionality of a WeSpeaker-ResNet34 voiceprint (vendored from
#: ``ratch.model.schema.VOICE_EMBED_DIM`` — the voice capability's contract).
VOICE_EMBED_DIM: int = 256

#: Below this turn duration the encoder's embeddings are unreliable (it was
#: trained on 5.0 s chunks).
MIN_TURN_DURATION_S: float = 0.5

#: Hard floor regardless of ``min_duration``: below ~0.1 s the fbank/ResNet
#: stack yields (almost) no frames and masked pooling degenerates.
_MIN_EMBED_SPAN_S: float = 0.1


class TurnSpan(BaseModel):
    """One diarized turn to embed, keyed back to its diarization row."""

    turn_id: int
    speaker_label: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class TurnBatchEncoder(Protocol):
    """Anything that embeds one batch of waveform slices → raw ``(n, dim)`` rows."""

    def embed_batch(self, waveforms: list[np.ndarray]) -> np.ndarray: ...


def extract_wav_16k_mono(source: Path, dest: Path, *, timeout: float = 1800.0) -> None:
    """Transcode ``source`` to a 16 kHz mono WAV at ``dest`` via ffmpeg.

    Raises :class:`RuntimeError` with the ffmpeg stderr tail on failure.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        "1",
        "-f",
        "wav",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s on {source}") from e
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg wav extraction failed (rc={proc.returncode}) on {source}: " + " | ".join(tail))


def load_wav_16k_mono(path: Path) -> np.ndarray:
    """Decode a 16 kHz mono PCM16 WAV (``extract_wav_16k_mono`` output) → float32 in [-1, 1].

    Raises :class:`ValueError` on any other WAV layout — the encoder's fbank
    front-end assumes exactly this format, so a silent resample would corrupt
    every embedding.
    """
    with wave.open(str(path), "rb") as wf:
        rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        if (rate, channels, width) != (TARGET_SAMPLE_RATE, 1, 2):
            raise ValueError(f"{path} is not {TARGET_SAMPLE_RATE} Hz mono PCM16 (rate={rate}, channels={channels}, sample_width={width})")
        pcm = wf.readframes(wf.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalization → float32 (the encoder's outputs are not unit-norm)."""
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return (vectors / np.maximum(norms, 1e-12)).astype(np.float32)


def clamped_sample_span(start: float, end: float, *, sample_rate: int, n_samples: int) -> tuple[int, int]:
    """A turn's ``[start, end)`` seconds → sample indices clamped to the decoded wav."""
    lo = max(0, round(start * sample_rate))
    hi = min(n_samples, round(end * sample_rate))
    return lo, hi


def duration_sorted_batches(lengths: Sequence[int], batch_size: int) -> list[list[int]]:
    """Indices ``0..len(lengths)`` grouped into length-sorted batches of ≤ ``batch_size``.

    Sorting puts similar-length turns in the same batch, minimising the
    zero-padding a padded batch carries (padding skews the encoder's fbank
    mean-centering, which the pooling ``weights`` mask cannot undo).
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    return [order[i : i + batch_size] for i in range(0, len(order), batch_size)]


def embed_turn_slices(
    encoder: TurnBatchEncoder,
    wav: np.ndarray,
    turns: Sequence[TurnSpan],
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    batch_size: int = 32,
    min_duration: float = MIN_TURN_DURATION_S,
) -> tuple[list[TurnSpan], np.ndarray]:
    """Slice + batch-embed one recording's turns → ``(kept_turns, embeddings)``.

    Skips any turn whose *clamped* span is shorter than ``min_duration`` (or the
    :data:`_MIN_EMBED_SPAN_S` hard floor) — too short to embed reliably. Row ``i``
    of the returned L2-normalized ``(len(kept), dim)`` float32 array is the
    embedding of ``kept_turns[i]``, in the original turn order regardless of the
    duration-sorted batching underneath.
    """
    min_samples = round(max(min_duration, _MIN_EMBED_SPAN_S) * sample_rate)
    kept: list[TurnSpan] = []
    slices: list[np.ndarray] = []
    for turn in turns:
        lo, hi = clamped_sample_span(turn.start, turn.end, sample_rate=sample_rate, n_samples=wav.shape[0])
        if hi - lo < min_samples:
            continue
        kept.append(turn)
        slices.append(wav[lo:hi])
    if not kept:
        return [], np.zeros((0, VOICE_EMBED_DIM), dtype=np.float32)

    batches = duration_sorted_batches([s.shape[0] for s in slices], batch_size)
    first = encoder.embed_batch([slices[i] for i in batches[0]])
    out = np.empty((len(kept), first.shape[1]), dtype=np.float32)
    out[batches[0]] = first
    for batch in batches[1:]:
        out[batch] = encoder.embed_batch([slices[i] for i in batch])
    return kept, l2_normalize(out)
