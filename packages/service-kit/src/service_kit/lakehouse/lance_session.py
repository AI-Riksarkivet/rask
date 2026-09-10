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

import logging
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import lance


#: Where a container states its own memory limit. v2 first — it is what cgroup-v2 hosts (k3s included)
#: expose — with the v1 path kept because a reader that knows one layout silently reports "unlimited"
#: on the other, and "unlimited" is the answer that disables the clamp below.
_CGROUP_V2 = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

#: The share of the container a process may spend on Lance caches by DEFAULT. Deliberately below a
#: half: the caps are LRU soft bounds (see the module docstring), so this is the size the cache grows
#: TOWARD, and it has to leave room for the working set that is doing the growing — the reconcile scan
#: holds its own structures across 93 buckets while the session fills.
_DEFAULT_CACHE_FRACTION = 0.4


log = logging.getLogger(__name__)


def cache_budget_bytes(*, fraction: float = _DEFAULT_CACHE_FRACTION) -> int | None:
    """The bytes THIS container can afford to spend on caches, or ``None`` when nothing constrains it.

    Read from the cgroup rather than configured, because a literal cannot track a chart value:
    `resources.limits.memory` can move without anyone revisiting a constant in Python, and the failure
    that follows is an OOMKill with no line to blame.

    ``None`` for an unconstrained process — a laptop, a CI runner, a container with no limit — so the
    caller falls back to whatever it was configured with. That is the honest answer for a process
    nobody has bounded, and it is why this returns an Optional rather than a very large number.
    """
    for path in (_CGROUP_V2, _CGROUP_V1):
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if raw == "max":  # cgroup v2's spelling of "no limit"
            return None
        try:
            limit = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports a sentinel near 2^63 for "unlimited"; anything that large is not a budget.
        if limit <= 0 or limit >= (1 << 62):
            return None
        return int(limit * fraction)
    return None


def affordable_cache_bytes(metadata_cache_bytes: int, index_cache_bytes: int, *, fraction: float = _DEFAULT_CACHE_FRACTION) -> tuple[int, int]:
    """The requested caps, reduced proportionally if this container cannot afford their sum.

    AN OPERATOR MAY ALWAYS ASK FOR LESS; what they may not do is ask for more than the container holds
    and have the process agree. Measured 2026-09-10: maintenance asked for 128 MB + 256 MB inside a
    512Mi pod whose baseline is 153Mi, and was OOMKilled after a reconcile pass warmed the session —
    the one service in the estate that HAD a bounded session, bounded above its own headroom.

    Reduced PROPORTIONALLY rather than truncated to a ceiling, because the ratio between the two caps
    is the caller's statement about its workload (maintenance wants twice as much index as metadata),
    and a clamp that flattened it would silently re-tune a service it knows nothing about.
    """
    budget = cache_budget_bytes(fraction=fraction)
    requested = metadata_cache_bytes + index_cache_bytes
    if budget is None or requested <= budget:
        return metadata_cache_bytes, index_cache_bytes
    scale = budget / requested
    metadata, index = int(metadata_cache_bytes * scale), int(index_cache_bytes * scale)
    _log_clamp(requested, metadata + index, budget, fraction)
    return metadata, index


@cache
def _log_clamp(requested: int, granted: int, budget: int, fraction: float) -> None:
    """Say once, per distinct outcome, that a configured cache was reduced to fit its container.

    A clamp nobody can see is indistinguishable from a setting nobody applied: an operator who
    configures 256 MB and silently receives 137 MB cannot tell this ran from a typo in their values
    file. So it is logged — but through a cached helper, because `affordable_cache_bytes` is called
    per session construction and a line per call would bury it.

    THE CACHE IS ON THE LOGGING, DELIBERATELY, AND NOT ON THE MEASUREMENT. `cache_budget_bytes` reads
    the cgroup live every time: Kubernetes supports in-place pod resize, so a cached limit would leave
    a resized container budgeting against a number that is no longer true — the exact drift reading
    the cgroup exists to prevent. The read is one small file and the session it feeds is itself
    `@cache`d, so live costs nothing.
    """
    log.info(
        "lance_cache_clamped_to_container",
        extra={"requested_bytes": requested, "granted_bytes": granted, "container_budget_bytes": budget, "fraction": fraction},
    )


@cache
def lance_session(metadata_cache_bytes: int, index_cache_bytes: int) -> lance.Session:
    """The process-wide session for the given caps — int-keyed, so equal caps share one session.

    ``Session``'s kwargs are STRICT — a typo raises ``TypeError`` rather than silently no-opping — so a
    misconfigured cap fails at first use, loudly, rather than running with a cache size nobody set.
    """
    import lance

    return lance.Session(
        metadata_cache_size_bytes=metadata_cache_bytes,
        index_cache_size_bytes=index_cache_bytes,
    )
