"""Row identity: the ONE place ``id`` is derived. Everything joins on this.

``id = sha256(key)`` was computed inline in the worker, and the anti-join was about to grow a
second copy in enumerate — two copies of an identity rule is how the enumerate skips a row the
worker would have written, silently. One function, imported by both.

THE TOKEN IS PART OF THE IDENTITY (owner ruling 2026-08-07: sinks replace objects in place). Same
key + same etag -> same id -> the anti-join skips it; replaced object -> new etag -> new id -> the
new bytes land as a NEW row while the old row stays. Bronze is history: both versions remain
queryable, and lineage records when each was witnessed. A token-less source (local-dir) keeps
``sha256(key)`` — snapshot semantics, byte-identical to every id this plane has ever written, so
the change is additive for existing tables.

The NUL separator prevents ambiguity: ``("ab", "c")`` and ``("a", "bc")`` must not collide, and a
key cannot contain NUL.
"""

from __future__ import annotations

import hashlib


def unit_id(key: str, token: str | None = None) -> int:
    """The bronze ``id`` for a source object — int64, deterministic, collision-separated."""
    material = key if token is None else f"{key}\x00{token}"
    return int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big", signed=True)
