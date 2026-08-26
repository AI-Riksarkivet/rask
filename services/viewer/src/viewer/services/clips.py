"""Clip excerpts with MP3 audio — the codec workaround for webview hosts.

Ported from the pre-split ``backend/media/clips.py`` unchanged in mechanics
(ffmpeg loopback over the backend's own Range-streaming media endpoint, disk
cache capped at 50 clips, one build at a time); only the cache key generalized
so multiple datasets can't collide on a doc id.

VS Code's webview Chromium ships without the AAC decoder (microsoft/vscode
#167685), so archive H.264+AAC files play silently inside MCP apps. This module
cuts the requested window out of the source (read seekably via the media
endpoint, since media lives in Lance blobs, not files) and re-encodes audio to
MP3, which every webview supports. Results are small complete MP4s served with
normal Range support, cached on disk and evicted oldest-first.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from service_kit.exceptions import ServiceUnavailableError


logger = logging.getLogger(__name__)

CACHE_DIR = Path(tempfile.gettempdir()) / "media-clip-cache"
MAX_CLIP_S = 660.0  # 2×(max transcript half-window) + slack
_MAX_CACHED = 50
_FFMPEG_TIMEOUT_S = 120

#: How many clips may transcode at once. Still small — the box's CPU belongs to the model servers and
#: a clip is seconds-cheap — but a NUMBER rather than the word "one", so the bound is stated.
MAX_CONCURRENT_BUILDS = 2

#: How coarsely a window is keyed, in milliseconds. The key was `int(lo * 1000)`, so `hi=1.001` and
#: `hi=1.002` were different files: a caller varying the third decimal never hit the cache and every
#: request became a fresh libx264 transcode. That is what turned a queue into a weapon. 250 ms is
#: below the threshold at which a viewer perceives a different excerpt and far above the noise a
#: client's own float arithmetic produces.
_CLIP_QUANTUM_MS = 250

# BOUNDED, not merely serialized. This was one `threading.Lock` and every waiter blocked on it —
# from a plain `def` route, so each waiter also held a FastAPI threadpool thread for as long as the
# queue ahead of it took, with a 120 s ffmpeg timeout at the head. 41 concurrent requests exhausted
# the pool while `/livez` stayed green, because liveness does not know the pool is gone.
#
# A semaphore with a non-blocking acquire turns that queue into a REFUSAL: the caller gets 503 and
# their thread returns immediately. Serializing was never the point — publish is made race-free by
# the tmp+rename below, not by the lock.
_build_slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)


class ClipBusyError(RuntimeError):
    """Every build slot is taken. The caller should retry, not wait — see `_build_slots`."""


class BuildSlot:
    """One acquired build slot, released explicitly or by leaving the `with` block."""

    def __init__(self) -> None:
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            _build_slots.release()

    def __enter__(self) -> BuildSlot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_build_slot() -> BuildSlot:
    """Take a build slot or refuse. NEVER blocks — blocking is the defect.

    Raises `ClipBusyError` when the estate is already transcoding `MAX_CONCURRENT_BUILDS` clips.
    """
    if not _build_slots.acquire(blocking=False):
        raise ClipBusyError(f"all {MAX_CONCURRENT_BUILDS} clip build slots are busy")
    return BuildSlot()


def clip_cache_path(cache_key: str, lo: float, hi: float) -> Path:
    """Deterministic cache file for one (dataset+doc, window). ``cache_key`` is
    built from the dataset id (a directory stem) and the pattern-whitelisted
    doc key, so the name can't traverse.

    The window is QUANTIZED to ``_CLIP_QUANTUM_MS``. Keyed to the millisecond, this
    was a free miss generator: ``hi=1.001`` and ``hi=1.002`` named different files, so
    a caller varying the third decimal transcoded on every request. Coarse enough that
    client-side float noise collapses onto one clip, fine enough that two windows a
    person could tell apart never do."""
    lo_ms = int(lo * 1000) // _CLIP_QUANTUM_MS
    hi_ms = int(hi * 1000) // _CLIP_QUANTUM_MS
    return CACHE_DIR / f"{cache_key}_{lo_ms}_{hi_ms}.mp4"


def evict_old_clips(cache_dir: Path, keep: int = _MAX_CACHED) -> None:
    """Drop the oldest cached clips beyond ``keep`` (by mtime)."""
    clips = sorted(cache_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in clips[keep:]:
        stale.unlink(missing_ok=True)


def build_clip(source_url: str, cache_key: str, lo: float, hi: float) -> Path:
    """Return the cached clip for the window, transcoding it on first request.

    ``-ss`` before ``-i`` on an HTTP input seeks via Range requests; video is
    re-encoded (not copied) so the output starts frame-accurately at ``lo``
    and the viewer's ``media_offset_s`` mapping holds.
    """
    out = clip_cache_path(cache_key, lo, hi)
    if out.exists():
        return out
    with acquire_build_slot():
        if out.exists():  # built while we waited for a slot
            return out
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{lo:.3f}",
            "-i",
            source_url,
            "-t",
            f"{hi - lo:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            "-movflags",
            "+faststart",
            "-y",
            str(tmp),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            tmp.unlink(missing_ok=True)
            raise ServiceUnavailableError("clip transcode timed out") from exc
        if proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            logger.warning("ffmpeg clip build failed: %s", proc.stderr[-500:])
            raise ServiceUnavailableError("clip transcode failed")
        tmp.rename(out)
        evict_old_clips(CACHE_DIR)
    return out
