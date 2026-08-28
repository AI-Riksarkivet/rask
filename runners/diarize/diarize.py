"""In-process speaker diarization → ``speaker_turns.lance``.

Runs pyannote's ``speaker-diarization-community-1`` pipeline over each source
video and writes its speaker turns to a separate append-only Lance table
(:data:`runners.diarize.schema.SPEAKER_TURNS_SCHEMA`). Kept separate from a wide
chunk-centric table for the same reason per-chunk frames are: one video's turns
are produced as a unit and we never ``merge_insert`` against a wide schema.

The pyannote pipeline (and its underlying torch models) load once into the
process and the GPU; loading it per video would dominate the wall-clock. We
therefore expose a :class:`Diarizer` that holds the loaded pipeline and is
reused across videos. Each video is first transcoded to 16 kHz mono WAV via
ffmpeg (what the pyannote models expect) into a temp file that is deleted after
inference.

pyannote 4.x returns a ``DiarizeOutput`` rather than a bare ``Annotation``; we
reach through ``.speaker_diarization`` when ``.itertracks`` isn't present so the
same code works against both the 3.x and 4.x return shapes. The HF token is read
from the ambient cached credentials (``huggingface-cli login`` / ``HF_TOKEN``);
this module never takes a token argument.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from runners.diarize.wav import extract_wav_16k_mono


logger = logging.getLogger(__name__)


#: HF model id for the diarization pipeline (validated working in this venv).
DEFAULT_PIPELINE: str = "pyannote/speaker-diarization-community-1"


class SpeakerTurn(BaseModel):
    """One diarized turn in absolute video seconds.

    ``speaker_label`` is pyannote's per-video local label ("SPEAKER_00", …),
    stable only within a single video.
    """

    speaker_label: str
    start: float
    end: float


class Diarizer:
    """Holds a loaded pyannote pipeline and diarizes videos one at a time.

    Construct **once** and reuse across videos — loading the pipeline and moving
    it onto the GPU is the expensive part. Mirrors the validated snippet:

    ```python
    pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")
    pipe.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
    out = pipe(wav_path_16k_mono)
    ann = out if hasattr(out, "itertracks") else getattr(out, "speaker_diarization", out)
    turns = [(t.start, t.end, spk) for t, _, spk in ann.itertracks(yield_label=True)]
    ```
    """

    def __init__(self, *, model: str = DEFAULT_PIPELINE) -> None:
        import torch
        from pyannote.audio import Pipeline

        pipe = Pipeline.from_pretrained(model)
        if pipe is None:
            raise RuntimeError(
                f"Pipeline.from_pretrained({model!r}) returned None — check the HF token "
                "(huggingface-cli login / HF_TOKEN) and that the model terms are accepted."
            )
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        pipe.to(device)
        logger.info("loaded diarization pipeline %s on %s", model, device)
        self._pipe = pipe

    def diarize(self, source: Path, *, ffmpeg_timeout: float = 1800.0) -> list[SpeakerTurn]:
        """Diarize one video → turns sorted by ``start`` (absolute seconds).

        Transcodes ``source`` to a temp 16 kHz mono WAV, runs the pipeline, and
        deletes the WAV before returning.
        """
        with tempfile.TemporaryDirectory(prefix="diarize-") as tmp:
            wav = Path(tmp) / "audio_16k_mono.wav"
            extract_wav_16k_mono(source, wav, timeout=ffmpeg_timeout)
            out = self._pipe(str(wav))

        # pyannote 4.x returns a DiarizeOutput; 3.x returns a bare Annotation.
        ann = out if hasattr(out, "itertracks") else getattr(out, "speaker_diarization", out)
        turns = [SpeakerTurn(speaker_label=spk, start=float(seg.start), end=float(seg.end)) for seg, _, spk in ann.itertracks(yield_label=True)]
        turns.sort(key=lambda t: t.start)
        return turns


def existing_doc_ids(turns_path: Path) -> set[str]:
    """The ``doc_id`` values already present in ``speaker_turns.lance``.

    Returns an empty set when the table is absent or empty. Used to make
    ``extract-speaker-turns`` resumable at video granularity.
    """
    import lance

    if not turns_path.exists():
        return set()
    ds = lance.dataset(str(turns_path))
    if ds.count_rows() == 0:
        return set()
    col = ds.to_table(columns=["doc_id"])["doc_id"].to_pylist()
    return set(col)


def write_speaker_turns(
    turns_path: Path,
    rows: Iterable[tuple[str, list[SpeakerTurn]]],
    *,
    create: bool,
) -> int:
    """Append diarized turns to the ``speaker_turns`` Lance table; returns rows written.

    ``rows`` is an iterable of ``(doc_id, turns)`` — one entry per video. ``turn_id``
    is assigned per video as the ``enumerate`` index over that video's turns (which
    :meth:`Diarizer.diarize` already returns sorted by ``start``). ``create=True``
    makes the first flush create/overwrite the table (fresh DB or ``--all`` rebuild);
    otherwise the first flush appends to the existing table. Videos that produced no
    turns are skipped (no rows written for them).
    """
    import pyarrow as pa

    from runners.diarize.dataset import append_rows, overwrite_dataset
    from runners.diarize.schema import SPEAKER_TURNS_SCHEMA

    n_written = 0
    first_write = create

    for doc_id, turns in rows:
        if not turns:
            continue
        table = pa.table(
            {
                "doc_id": pa.array([doc_id] * len(turns), pa.string()),
                "turn_id": pa.array(list(range(len(turns))), pa.int32()),
                "speaker_label": pa.array([t.speaker_label for t in turns], pa.string()),
                "start": pa.array([t.start for t in turns], pa.float32()),
                "end": pa.array([t.end for t in turns], pa.float32()),
            },
            schema=SPEAKER_TURNS_SCHEMA,
        )
        if first_write:
            overwrite_dataset(turns_path, table)
        else:
            append_rows(turns_path, table)
        first_write = False
        n_written += len(turns)

    return n_written
