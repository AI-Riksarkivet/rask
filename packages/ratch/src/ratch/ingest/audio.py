"""Resolve source media paths and compute their URIs / MIME types.

Videos are **referenced** (not copied) from the Lance ``documents`` table:
the ``media_blob`` column is a Lance Blob V2 *External* column whose values
are URI strings, written via ``blob_array([uri, …])`` (see
:mod:`ratch.ingest`). On read, ``ds.take_blobs("media_blob", …)`` returns
lazy, range-readable ``BlobFile`` handles. The table stays a small portable
catalog; the actual MP4 bytes live wherever the URI points:

- Local dev: ``file:///abs/path/input/T0001417_00001.mp4``
- HF bucket: ``hf://buckets/you/videos/T0001417_00001.mp4``
- S3:        ``s3://bucket/videos/T0001417_00001.mp4``

This module only builds those URI strings and guesses MIME types; the Blob
V2 write wiring lives in :mod:`ratch.ingest`, the Range-read path in the
backend (``backend/app.py`` ``/api/media``).
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path


logger = logging.getLogger(__name__)


def resolve_source(audio_path: str, audio_root: str | Path | None) -> Path | None:
    """Resolve a transcript's ``audio_path`` against an optional root directory.

    Returns ``None`` (and logs a warning) when the file isn't found.
    """
    if audio_root is None or Path(audio_path).is_absolute():
        p = Path(audio_path)
    else:
        p = Path(audio_root) / audio_path

    if not p.exists():
        logger.warning("media source not found: %s", p)
        return None
    return p


def guess_mime(path: str | Path) -> str:
    """Guess MIME from filename extension, falling back to ``application/octet-stream``."""
    mime, _ = mimetypes.guess_type(Path(path).name)
    return mime or "application/octet-stream"


def compose_media_uri(
    *,
    audio_path: str,
    source_path: Path | None,
    base_uri: str | None,
) -> str | None:
    """Compose the external URI for a source media file.

    Precedence:
      1. ``base_uri`` (e.g. ``hf://buckets/you/videos/``) + ``audio_path``
         basename → treat the transcript's filename as the key.
      2. ``source_path`` resolved to an absolute ``file://`` URI.
      3. ``None`` (media not locatable).
    """
    if base_uri:
        sep = "" if base_uri.endswith("/") else "/"
        return f"{base_uri}{sep}{Path(audio_path).name}"
    if source_path is not None:
        return source_path.resolve().as_uri()
    return None
