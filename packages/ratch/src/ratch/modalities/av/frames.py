"""Single-frame ffmpeg extraction for chunk-level visual indexing.

Used by ``ratch extract-chunk-frames`` to grab one representative JPEG per
transcript chunk at ``chunk.start``. This module only *produces* the JPEG
bytes (piped from ffmpeg over stdout — no temp files, no manifest CSV); the
CLI then writes them into the separate append-only ``chunk_frames`` Lance
table (Blob V2 Inline — see :data:`ratch.model.schema.CHUNK_FRAMES_SCHEMA`). That
table is kept separate from ``chunks`` because Lance 4.0's ``merge_insert``
crashes on the wide ``chunks`` schema when backfilling blob columns.

ffmpeg is invoked with ``-ss <t>`` *before* ``-i <src>`` for fast-seek;
that's slightly less accurate than slow-seek (decodes from the previous
keyframe forward, then drops frames) but plenty for press-conf footage
where the speaker barely moves between keyframes. Output is piped to
stdout as MJPEG so we never touch disk.

CPU-bound ffmpeg startup is the bottleneck (~80–120ms/chunk); the Ray
``extract_frames`` stage (ratch pipeline run) parallelizes it across actors.
"""

from __future__ import annotations

import logging
import struct
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


# Width is a Qwen3-VL-friendly default — 448px wide preserves enough
# detail for visual retrieval, JPEG ~50–80 KB at q=4 fits Lance's Blob V2
# Inline 64 KB threshold most of the time, spilling to sidecar otherwise.
DEFAULT_FRAME_WIDTH: int = 448
DEFAULT_JPEG_QUALITY: int = 4  # ffmpeg `-q:v` (1=best, 31=worst); 4 ~= JPEG q≈85


class ExtractedFrame(BaseModel):
    """Result of one ffmpeg extraction. ``jpeg_bytes`` is empty on failure."""

    doc_id: str
    speech_id: int
    chunk_id: int
    frame_idx: int = 0
    time_sec: float
    jpeg_bytes: bytes
    width: int
    height: int
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────
# JPEG dimensions parser (avoids round-tripping through PIL just for size)
# ─────────────────────────────────────────────────────────────────────


def _jpeg_dimensions(jpeg: bytes) -> tuple[int, int]:
    """Return (width, height) from a JPEG SOF0/SOF2 marker.

    Faster + lighter than `PIL.Image.open(BytesIO(jpeg)).size` because
    we never decode pixel data, and `[multimodal]` may not be installed
    when the *extraction* CLI runs (it's CPU-only, no model needed).
    Returns (0, 0) if parsing fails — non-fatal, dimensions are stored
    only as metadata.
    """
    try:
        i = 2  # skip SOI
        while i < len(jpeg) - 9:
            if jpeg[i] != 0xFF:
                return 0, 0
            marker = jpeg[i + 1]
            # Standalone markers / restart markers
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", jpeg[i + 2 : i + 4])[0]
            # SOF0/SOF1/SOF2 carry the actual frame dimensions
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", jpeg[i + 5 : i + 9])
                return w, h
            i += 2 + seg_len
    except Exception:
        pass
    return 0, 0


# ─────────────────────────────────────────────────────────────────────
# Single-chunk extraction
# ─────────────────────────────────────────────────────────────────────


def extract_chunk_frame(
    *,
    source: Path,
    time_sec: float,
    width: int = DEFAULT_FRAME_WIDTH,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    timeout: float = 30.0,
) -> tuple[bytes, int, int]:
    """Pull a single JPEG frame at ``time_sec`` from ``source`` via ffmpeg.

    Returns (jpeg_bytes, width, height). Raises :class:`RuntimeError`
    with the ffmpeg stderr tail if ffmpeg fails (including missing source).
    """
    # `-ss` before `-i` triggers fast-seek (input-stream demuxer level).
    # `-frames:v 1` exits after writing one frame to stdout.
    # `-f mjpeg` forces the muxer when the destination is `pipe:1`.
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_sec:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        str(jpeg_quality),
        "-f",
        "mjpeg",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s on {source}") from e

    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}) on {source} @ {time_sec:.2f}s: " + " | ".join(tail))

    jpeg = proc.stdout
    w, h = _jpeg_dimensions(jpeg)
    return jpeg, w, h


def sample_times(start: float, end: float, every_seconds: float) -> list[float]:
    """Frame timestamps for one chunk.

    ``[start]`` for a single representative frame; with ``every_seconds > 0``,
    ``start, start+every, …`` up to (not past) ``end`` — always at least one.
    """
    if every_seconds <= 0 or end <= start:
        return [start]
    times: list[float] = []
    t = start
    while t < end:
        times.append(round(t, 3))
        t += every_seconds
    return times or [start]


def write_chunk_frames(
    frames_path: Path,
    frames: Iterable[ExtractedFrame],
    *,
    create: bool,
    batch: int = 2000,
    progress: Callable[[int], None] | None = None,
) -> tuple[int, int]:
    """Append extracted frames to the chunk_frames Lance table; returns (ok, failed).

    Flushes every ``batch`` frames. ``create=True`` makes the first flush
    create/overwrite the table (use after a ``--all`` drop or on a fresh DB);
    otherwise the first flush appends to the existing table. Frames whose
    extraction failed (``error`` set / no bytes) are counted, logged (first few),
    and skipped — never written.
    """
    import pyarrow as pa
    from lance import blob_array

    from ratch.core.dataset import append_rows, overwrite_dataset
    from ratch.model.schema import CHUNK_FRAMES_SCHEMA

    n_ok = n_fail = 0
    first_write = create
    pending: list[ExtractedFrame] = []

    def flush() -> None:
        nonlocal n_ok, first_write
        good = [f for f in pending if f.jpeg_bytes]
        pending.clear()
        if not good:
            return
        table = pa.table(
            {
                "doc_id": pa.array([f.doc_id for f in good], pa.string()),
                "speech_id": pa.array([f.speech_id for f in good], pa.int32()),
                "chunk_id": pa.array([f.chunk_id for f in good], pa.int32()),
                "frame_idx": pa.array([f.frame_idx for f in good], pa.int32()),
                "frame_blob": blob_array([f.jpeg_bytes for f in good]),
                "frame_mime": pa.array(["image/jpeg"] * len(good), pa.string()),
                "frame_width": pa.array([f.width for f in good], pa.int32()),
                "frame_height": pa.array([f.height for f in good], pa.int32()),
            },
            schema=CHUNK_FRAMES_SCHEMA,
        )
        if first_write:
            overwrite_dataset(frames_path, table)
        else:
            append_rows(frames_path, table)
        first_write = False
        n_ok += len(good)

    for frame in frames:
        if frame.error:
            n_fail += 1
            if n_fail <= 5:
                logger.warning("frame extraction failed: %s@%.2fs — %s", frame.doc_id, frame.time_sec, frame.error)
        pending.append(frame)
        if len(pending) >= batch:
            flush()
        if progress is not None:
            progress(1)
    flush()
    return n_ok, n_fail
