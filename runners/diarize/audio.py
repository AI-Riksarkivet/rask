"""Resolve a source media path for this runner's stage.

Vendored from ``ratch.ingest.audio`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, moves 10 and 11). It is a COPY rather than an import,
and that is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` for the cu128 torch
stack, while every platform package (``ratch``, ``service-kit``, ``ray-kit``) is
``>=3.13``. No platform package can be imported here at all.

Only :func:`resolve_source` travels — the stage reads bytes off a resolved local
path. The origin's ``guess_mime`` and ``compose_media_uri`` do NOT: those build
the Blob V2 External URI strings (``file://`` / ``hf://`` / ``s3://``) written
into the platform's ``documents`` table, which is the catalog's business and not
a workload's. This runner never called either.
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
