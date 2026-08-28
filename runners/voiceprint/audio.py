"""Resolve a document's source media path against an optional root directory.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/ingest/audio.py``, narrowed to :func:`resolve_source`. A COPY because the
runner is sealed (``requires-python >=3.10,<3.13``; platform packages are ``>=3.13``).

TAKEN: :func:`resolve_source` — stdlib-only, and the one function the actor calls to turn a row's
``audio_path`` plus ``RunnerContext.audio_root`` into a file it can transcode.
LEFT BEHIND: ``guess_mime`` and ``compose_media_uri``. Those build the ``media_blob`` URI strings
for the corpus-wide ``documents`` table — Lance Blob V2 External values written at INGEST, which is
the platform's side of the seam. This runner reads media and writes vectors; it never authors a
document row, so both are uncalled here.
"""

from __future__ import annotations

import logging
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
