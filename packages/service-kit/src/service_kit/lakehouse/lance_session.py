"""One process-wide, size-bounded Lance session (#102 — the H1 cache defect's fix).

Every bare ``lance.dataset(uri)`` mints a FRESH cache pair at Lance's defaults — 1 GiB metadata +
6 GiB index ceilings — and discards it with the handle. A service that opens datasets per tick (the
maintenance sweep) or per version (the orphan scan's referenced-set walk) pays the allocation and
loses the cache exactly when the next open would have hit it. Measured on pylance 9.0.0: ten
version-opens against a shared session grow ``session.size_bytes()`` from 168 to ~75k while the
same opens without one leave it flat — the cache simply never engaged.

The fix is ONE ``lance.Session`` per process, capped explicitly:

- The caps are **LRU soft bounds**, not hard ceilings — never assert a hard total against them.
- Session cache keys are ``(uri, version, etag)``, so a compaction bumping a dataset's version
  writes NEW keys: there is no freshness contract to design and no stale-read window to reason
  about. This is also why a shared session is safe for a service that MUTATES what it opens.
- ``lance.Session`` is thread-safe (verified: 8 threads × 50 open+checkout, zero errors), which
  the maintenance service needs — its sweep and reconcile crons hold separate locks and overlap.

NOT a dataset-handle cache. A cached handle pins a version (the viewer's ``DatasetRegistry``
trade, right for a read-only corpus); per-tick reopens are CORRECT for a mutating service — what
was wrong was each reopen minting and discarding gigabyte-scale cache ceilings.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import lance


@cache
def lance_session(metadata_cache_bytes: int, index_cache_bytes: int) -> lance.Session:
    """The process-wide session for the given caps — int-keyed, so equal caps share one session.

    ``Session``'s kwargs are strict on pylance 9.0.0 (a typo raises ``TypeError`` rather than
    silently no-opping), so a misconfigured cap fails at first use, loudly.
    """
    import lance

    # pylance 9.0.0 ships no __init__ stub for Session, so ty resolves these against object() —
    # the kwargs are real and STRICT (verified live: Session(metadata_cache_size_bytes=..) works,
    # a typo raises TypeError).
    return lance.Session(
        metadata_cache_size_bytes=metadata_cache_bytes,  # ty: ignore[unknown-argument]
        index_cache_size_bytes=index_cache_bytes,  # ty: ignore[unknown-argument]
    )
