"""Generate JPEG thumbnails for media files via ffmpeg.

Exposed as ``ratch thumbnail``. For each video/audio file under
``--input-dir`` we extract a single frame at ``--at`` seconds (default 5s)
and write it to ``<output_dir>/<stem>.jpg``. Existing files are skipped, so
the command is idempotent/resumable.

Audio-only files (mp3/wav/…) fall back to a waveform render so the gallery
still gets a visual; if ffmpeg refuses, they end up with no thumbnail, which
is fine — the column stays NULL.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ratch.errors import RatchError


logger = logging.getLogger(__name__)

# File extensions we try to thumbnail. Everything else is skipped silently.
VIDEO_EXTS: frozenset[str] = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv"})
AUDIO_EXTS: frozenset[str] = frozenset({".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus"})
MEDIA_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS


def _extract_video_frame(src: Path, dest: Path, *, at_sec: float, width: int) -> bool:
    """Pull a single frame at ``at_sec`` and resize to ``width`` (keep AR)."""
    # Fast seek (-ss before -i) is less accurate but ~100× faster on long files.
    # For long press-conf videos, accuracy doesn't matter for a thumbnail.
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        str(at_sec),
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "4",
        str(dest),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return r.returncode == 0 and dest.exists()


def _render_waveform(src: Path, dest: Path, width: int) -> bool:
    """Fallback for audio-only files: render a waveform PNG-as-JPEG."""
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        f"aformat=channel_layouts=mono,showwavespic=s={width}x120:colors=#6ca6ff",
        "-frames:v",
        "1",
        str(dest),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return r.returncode == 0 and dest.exists()


def generate_thumbnails(
    *,
    input_dir: Path,
    output_dir: Path,
    at_sec: float = 5.0,
    width: int = 480,
    overwrite: bool = False,
) -> dict[str, list[str]]:
    """Walk ``input_dir`` recursively and generate a JPEG per media file.

    Returns ``{status: [stem, ...]}`` with ``status`` in
    ``{"ok", "skipped", "failed", "unsupported"}``.
    """
    if not input_dir.is_dir():
        raise RatchError(f"Input directory not found: {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, list[str]] = {}
    total = 0
    for src in sorted(input_dir.rglob("*")):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        if ext not in MEDIA_EXTS:
            continue
        total += 1
        dest = output_dir / f"{src.stem}.jpg"

        if dest.exists() and dest.stat().st_size > 0 and not overwrite:
            buckets.setdefault("skipped", []).append(src.stem)
            continue

        ok = False
        if ext in VIDEO_EXTS:
            ok = _extract_video_frame(src, dest, at_sec=at_sec, width=width)
        elif ext in AUDIO_EXTS:
            ok = _render_waveform(src, dest, width)

        status = "ok" if ok else "failed"
        buckets.setdefault(status, []).append(src.stem)
        logger.info("[%-7s] %s → %s", status, src.name, dest.name)

    logger.info("processed %s file(s) from %s/ → %s/", total, input_dir, output_dir)
    for status in sorted(buckets):
        logger.info("  %-10s %5s", status, len(buckets[status]))
    return buckets
