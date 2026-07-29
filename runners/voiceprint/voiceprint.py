"""In-process speaker-embedding (voiceprint) extraction → ``speaker_embeddings.lance``.

Used by ``ratch embed-speaker-turns`` to embed each diarized turn's voice with
pyannote community-1's **internal** WeSpeaker-ResNet34 encoder (256-d), loaded
standalone via ``Model.from_pretrained(..., subfolder="embedding")``. The raw
model outputs are *not* unit-norm; everything this module returns is
L2-normalized so plain cosine kNN is well-defined. Like :class:`.diarize.Diarizer`,
the HF token comes from the ambient cached credentials (``huggingface-cli login``
/ ``HF_TOKEN``); this module never takes a token argument.

The encoder was trained on 5.0 s chunks and is unreliable below ~0.5 s, so turns
shorter than ``min_duration`` are skipped (:data:`MIN_TURN_DURATION_S`). Turns
have arbitrary lengths, so a batch is zero-padded to its longest member and the
padded frames are masked out of the statistics pooling via the model's
``weights`` argument; batches are duration-sorted first so similar-length turns
share a batch and the padding (which the fbank mean-centering still sees) stays
minimal.

The slicing/batching/centroid logic is pure (numpy only, no torch) so it is
unit-testable against a fake encoder — only :class:`VoiceEncoder` touches the
model.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np
from pydantic import BaseModel
from ratch.modalities.av.wav import TARGET_SAMPLE_RATE, extract_wav_16k_mono


if TYPE_CHECKING:
    import lancedb

logger = logging.getLogger(__name__)


#: HF model id whose "embedding" subfolder holds the WeSpeaker-ResNet34 encoder.
DEFAULT_MODEL: str = "pyannote/speaker-diarization-community-1"

#: Subfolder of :data:`DEFAULT_MODEL` the standalone encoder is loaded from.
EMBEDDING_SUBFOLDER: str = "embedding"

#: Below this turn duration the encoder's embeddings are unreliable (it was
#: trained on 5.0 s chunks); ``embed-speaker-turns`` exposes it as
#: ``--min-turn-duration``.
MIN_TURN_DURATION_S: float = 0.5

#: Hard floor regardless of ``min_duration``: below ~0.1 s the fbank/ResNet
#: stack yields (almost) no frames and masked pooling degenerates.
_MIN_EMBED_SPAN_S: float = 0.1


class TurnSpan(BaseModel):
    """One diarized turn to embed, as read from ``speaker_turns.lance``.

    Unlike :class:`.diarize.SpeakerTurn` it carries the persisted ``turn_id`` so
    the written embedding row keys back to its speaker_turns row.
    """

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
    """Slice + batch-embed one video's turns → ``(kept_turns, embeddings)``.

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
        from ratch.model.schema import VOICE_EMBED_DIM

        return [], np.zeros((0, VOICE_EMBED_DIM), dtype=np.float32)

    batches = duration_sorted_batches([s.shape[0] for s in slices], batch_size)
    first = encoder.embed_batch([slices[i] for i in batches[0]])
    out = np.empty((len(kept), first.shape[1]), dtype=np.float32)
    out[batches[0]] = first
    for batch in batches[1:]:
        out[batch] = encoder.embed_batch([slices[i] for i in batch])
    return kept, l2_normalize(out)


def duration_weighted_centroid(embeddings: np.ndarray, durations: Sequence[float]) -> np.ndarray:
    """Duration-weighted mean of unit-norm turn embeddings, re-L2-normalized.

    ``embeddings`` is ``(n, dim)`` with one row per turn; ``durations`` the
    matching turn lengths in seconds (the weights). Returns a ``(dim,)`` float32
    unit vector — the per-speaker voiceprint stored in ``speakers.lance``.
    Raises :class:`ValueError` on empty/mismatched input, non-positive
    durations, or a degenerate (all-cancelling) centroid.
    """
    if embeddings.ndim != 2 or embeddings.shape[0] == 0:
        raise ValueError(f"expected a non-empty (n, dim) array, got shape {embeddings.shape}")
    if embeddings.shape[0] != len(durations):
        raise ValueError(f"{embeddings.shape[0]} embedding(s) vs {len(durations)} duration(s)")
    weights = np.asarray(durations, dtype=np.float64)
    if np.any(weights <= 0):
        raise ValueError("durations must be positive")
    centroid = (embeddings.astype(np.float64) * weights[:, None]).sum(axis=0) / weights.sum()
    norm = float(np.linalg.norm(centroid))
    if norm == 0.0:
        raise ValueError("degenerate centroid: weighted turn embeddings cancel out")
    return (centroid / norm).astype(np.float32)


class VoiceEncoder:
    """Holds the loaded WeSpeaker-ResNet34 encoder and embeds turns video by video.

    Construct **once** and reuse across videos — loading the model and moving it
    onto the GPU is the expensive part (mirrors :class:`.diarize.Diarizer`).
    """

    def __init__(self, *, model: str = DEFAULT_MODEL, device: str = "auto") -> None:
        import torch
        from pyannote.audio import Model

        encoder = Model.from_pretrained(model, subfolder=EMBEDDING_SUBFOLDER)
        if encoder is None:
            raise RuntimeError(
                f"Model.from_pretrained({model!r}, subfolder={EMBEDDING_SUBFOLDER!r}) returned "
                "None — check the HF token (huggingface-cli login / HF_TOKEN) and that the "
                "model terms are accepted."
            )
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = torch.device(device)
        encoder.to(self._device)
        encoder.eval()
        logger.info("loaded voice encoder %s/%s on %s", model, EMBEDDING_SUBFOLDER, self._device)
        self._model = encoder

    def embed_batch(self, waveforms: list[np.ndarray]) -> np.ndarray:
        """Forward one batch of waveform slices → raw ``(len(waveforms), 256)`` rows.

        Slices are zero-padded to the batch max; the pooling ``weights`` mask
        the padded frames out of the statistics (pyannote's ``StatsPool``
        interpolates the mask onto the model's frame resolution).
        """
        import torch

        # The wrapper's samples→frames method; cast because nn.Module.__getattr__
        # types every dynamic attribute as Tensor | Module.
        num_frames = cast(Callable[[int], int], self._model.num_frames)
        max_len = max(w.shape[0] for w in waveforms)
        batch = torch.zeros(len(waveforms), 1, max_len)
        weights = torch.zeros(len(waveforms), int(num_frames(max_len)))
        for i, w in enumerate(waveforms):
            batch[i, 0, : w.shape[0]] = torch.from_numpy(w)
            weights[i, : int(num_frames(w.shape[0]))] = 1.0
        with torch.inference_mode():
            embeddings = self._model(batch.to(self._device), weights=weights.to(self._device))
        return embeddings.cpu().numpy().astype(np.float32)

    def embed_turns(
        self,
        wav_path: Path,
        turns: Sequence[TurnSpan],
        *,
        batch_size: int = 32,
        min_duration: float = MIN_TURN_DURATION_S,
    ) -> tuple[list[TurnSpan], np.ndarray]:
        """Embed one video's turns → ``(kept_turns, L2-normalized (n, 256) array)``.

        ``wav_path`` must be the video's 16 kHz mono WAV (``extract_wav_16k_mono``
        output). Turns shorter than ``min_duration`` are skipped; row ``i``
        belongs to ``kept_turns[i]``.
        """
        wav = load_wav_16k_mono(wav_path)
        return embed_turn_slices(self, wav, turns, batch_size=batch_size, min_duration=min_duration)


def write_speaker_embeddings(
    emb_path: Path,
    rows: Iterable[tuple[str, list[TurnSpan], np.ndarray]],
    *,
    create: bool,
) -> int:
    """Append per-turn voice embeddings to a ``speaker_embeddings`` Lance table.

    ``rows`` is an iterable of ``(doc_id, kept_turns, embeddings)`` — one entry
    per video, with ``embeddings[i]`` belonging to ``kept_turns[i]`` (the
    :meth:`VoiceEncoder.embed_turns` contract). One table-write per video;
    ``create=True`` makes the first flush create/overwrite the table. Mirrors
    :func:`.diarize.write_speaker_turns`. Videos with no embeddable turns are
    skipped. Returns the number of rows written.
    """
    import pyarrow as pa
    from ratch.core.dataset import append_rows, overwrite_dataset
    from ratch.model.schema import SPEAKER_EMBEDDINGS_SCHEMA, VOICE_EMBED_DIM

    n_written = 0
    first_write = create

    for doc_id, turns, embeddings in rows:
        if not turns:
            continue
        if embeddings.shape != (len(turns), VOICE_EMBED_DIM):
            raise ValueError(f"{doc_id}: expected ({len(turns)}, {VOICE_EMBED_DIM}) embeddings, got {embeddings.shape}")
        flat = pa.array(embeddings.astype(np.float32).reshape(-1), pa.float32())
        table = pa.table(
            {
                "doc_id": pa.array([doc_id] * len(turns), pa.string()),
                "turn_id": pa.array([t.turn_id for t in turns], pa.int32()),
                "speaker_label": pa.array([t.speaker_label for t in turns], pa.string()),
                "start": pa.array([t.start for t in turns], pa.float32()),
                "end": pa.array([t.end for t in turns], pa.float32()),
                "duration": pa.array([t.duration for t in turns], pa.float32()),
                "embedding": pa.FixedSizeListArray.from_arrays(flat, VOICE_EMBED_DIM),
            },
            schema=SPEAKER_EMBEDDINGS_SCHEMA,
        )
        if first_write:
            overwrite_dataset(emb_path, table)
        else:
            append_rows(emb_path, table)
        first_write = False
        n_written += len(turns)

    return n_written


def embed_videos(
    encoder: VoiceEncoder,
    videos: Sequence[tuple[str, Path]],
    turns_by_doc: dict[str, list[TurnSpan]],
    *,
    batch_size: int,
    min_turn_duration: float,
    ffmpeg_timeout: float,
    progress: Callable[[Sequence[tuple[str, Path]]], Iterable[tuple[str, Path]]] | None = None,
) -> Iterator[tuple[str, list[TurnSpan], np.ndarray]]:
    """Decode each video to 16 kHz mono WAV and embed its turns, video by video.

    Yields one ``(doc_id, kept_turns, embeddings)`` per video in the
    :func:`write_speaker_embeddings` contract. A video whose decode or embedding
    raises is logged and skipped so one bad video never kills the batch.
    ``progress`` wraps the iteration (e.g. ``tqdm``); identity when ``None``.
    """
    iterator = progress(videos) if progress is not None else videos
    for doc_id, src in iterator:
        try:
            with tempfile.TemporaryDirectory(prefix="ratch-voice-") as tmp:
                wav = Path(tmp) / "audio_16k_mono.wav"
                extract_wav_16k_mono(src, wav, timeout=ffmpeg_timeout)
                kept, embeddings = encoder.embed_turns(
                    wav,
                    turns_by_doc[doc_id],
                    batch_size=batch_size,
                    min_duration=min_turn_duration,
                )
        except Exception as e:
            logger.warning("voice embedding failed: %s (%s) — %s", doc_id, src, e)
            continue
        yield doc_id, kept, embeddings


def build_speakers(db: lancedb.DBConnection, db_path: Path) -> tuple[int, int]:
    """Aggregate per-turn voice embeddings → ``speakers.lance`` (overwrite).

    Groups the canonical ``speaker_embeddings`` table by ``(doc_id, speaker_label)``
    and writes one row per local speaker: turn count, total speech duration, and the
    duration-weighted, re-L2-normalized mean of its turn embeddings. ``speaker_cluster``
    starts at -1 for the later global-clustering pass; ``speaker_name`` starts NULL.
    The table is tiny, so each call rebuilds it wholesale. Returns
    ``(speaker_count, video_count)``.

    Raises :class:`ValueError` when ``speaker_embeddings`` is missing, empty, or no
    centroid could be computed.
    """
    import lance
    import pyarrow as pa
    from ratch.core.dataset import overwrite_dataset
    from ratch.model.schema import SPEAKERS_SCHEMA, VOICE_EMBED_DIM

    if "speaker_embeddings" not in db.list_tables().tables:
        raise ValueError(
            f"Table 'speaker_embeddings' not found in {db_path} — run "
            "`ratch embed-speaker-turns` first (and `ratch merge-speaker-embeddings` "
            "if it ran sharded)."
        )

    ds = lance.dataset(str(db_path / "speaker_embeddings.lance"))
    tbl = ds.to_table(columns=["doc_id", "speaker_label", "duration", "embedding"])
    if tbl.num_rows == 0:
        raise ValueError("speaker_embeddings is empty — run `ratch embed-speaker-turns` first.")

    doc_ids = tbl["doc_id"].to_pylist()
    labels = tbl["speaker_label"].to_pylist()
    durations = tbl["duration"].to_pylist()
    vectors = tbl["embedding"].combine_chunks().flatten().to_numpy(zero_copy_only=False).reshape(-1, VOICE_EMBED_DIM).astype(np.float32)

    groups: dict[tuple[str, str], list[int]] = {}
    for i, key in enumerate(zip(doc_ids, labels, strict=True)):
        groups.setdefault(key, []).append(i)

    out_keys: list[tuple[str, str]] = []
    out_n_turns: list[int] = []
    out_totals: list[float] = []
    centroids: list[np.ndarray] = []
    for (doc_id, label), idxs in sorted(groups.items()):
        turn_durations = [durations[i] for i in idxs]
        try:
            centroid = duration_weighted_centroid(vectors[idxs], turn_durations)
        except ValueError as e:
            logger.warning("skipping speaker %s/%s: %s", doc_id, label, e)
            continue
        out_keys.append((doc_id, label))
        out_n_turns.append(len(idxs))
        out_totals.append(float(sum(turn_durations)))
        centroids.append(centroid)

    if not centroids:
        raise ValueError("No speaker centroid could be computed — speaker_embeddings looks corrupt.")

    n = len(centroids)
    flat = pa.array(np.concatenate(centroids), pa.float32())
    speakers = pa.table(
        {
            "doc_id": pa.array([d for d, _ in out_keys], pa.string()),
            "speaker_label": pa.array([s for _, s in out_keys], pa.string()),
            "n_turns": pa.array(out_n_turns, pa.int32()),
            "total_duration": pa.array(out_totals, pa.float32()),
            "embedding": pa.FixedSizeListArray.from_arrays(flat, VOICE_EMBED_DIM),
            "speaker_cluster": pa.array([-1] * n, pa.int32()),
            "speaker_name": pa.array([None] * n, pa.string()),
        },
        schema=SPEAKERS_SCHEMA,
    )
    overwrite_dataset(db_path / "speakers.lance", speakers)

    speakers_tbl = db.open_table("speakers")
    try:
        speakers_tbl.create_scalar_index("doc_id", index_type="BTREE", replace=True)
        logger.info("built BTREE scalar index on speakers.doc_id")
    except Exception as e:
        logger.debug("scalar index (speakers.doc_id) skipped: %s", e)

    n_videos = len({d for d, _ in groups})
    return n, n_videos


def speaker_embeddings_indexes(emb_tbl: lancedb.table.Table) -> None:
    """(Re)build the canonical ``speaker_embeddings`` indexes: BTREE doc_id + vector.

    Shared by ``embed-speaker-turns`` (single-run) and ``merge-speaker-embeddings``.
    """
    from ratch.core.engine import ensure_vector_index

    try:
        emb_tbl.create_scalar_index("doc_id", index_type="BTREE", replace=True)
        logger.info("built BTREE scalar index on speaker_embeddings.doc_id")
    except Exception as e:
        logger.debug("scalar index (speaker_embeddings.doc_id) skipped: %s", e)
    # 256-d voice vectors → 16 sub-vectors (16 dims each); skips itself while
    # the table is smaller than num_partitions (flat search is fine until then).
    ensure_vector_index(emb_tbl, "embedding", num_partitions=256, num_sub_vectors=16)
