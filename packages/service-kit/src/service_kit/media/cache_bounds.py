"""Bounded eviction for the caches on :class:`~service_kit.media.state.AppState`.

TWO BOUNDS, AND THEY MEASURE DIFFERENT THINGS. The entry count is the LOOKUP bound — it keeps the
dict small and the key space from drifting. The byte total is the MEMORY bound, and it is the one
that decides whether the pod survives. A cache holding twelve entries is bounded by neither if each
entry is a hundred megabytes.

WHY THIS IS SHARED RATHER THAN WRITTEN TWICE. It already was written twice. `AppState` carries two
caches with the same version-keyed invalidation model — `search_cache` and `points_cache` — and only
the first got a byte ceiling (`search_cache_bytes = 64 MiB`), because someone measured the problem on
that one. The other kept `while len(cache) >= 12` under a comment that says "each is multi-MB"
without turning that into a bound: `available_modes` derives one space per declared embedding key, so
three spaces across four corpora is already twelve distinct keys, and at ~100 MB per Arrow payload
that is 1.2 GB resident in a pod the chart gives one replica. The viewer OOM-kills and every explorer
route dies with it.

Fixing the second twin by copying the first would leave the same shape for a third. So the eviction
is one function, and a cache added to `AppState` inherits both bounds instead of re-deciding them.

`max_bytes <= 0` disables the BYTE bound only, matching what `search_cache_bytes` already documents —
the two must not diverge on the meaning of their own settings.
"""

from __future__ import annotations


def evict_to_bounds[K, V](
    cache: dict[K, tuple[V, int]],
    *,
    max_entries: int,
    max_bytes: int,
    incoming_bytes: int = 0,
) -> None:
    """Evict oldest-first until `cache` has room for an entry of `incoming_bytes`.

    Insertion order is the LRU proxy both caches already relied on — a plain dict preserves it, and
    neither cache re-reads an entry often enough for true LRU to pay for itself.

    `incoming_bytes` is counted against the ceiling BEFORE the caller inserts, so the bound holds
    across the insert rather than one entry after it.

    An entry larger than the whole ceiling is NOT allowed to empty the cache on its way to not
    fitting: the loop stops when only the incoming entry would remain unaccommodated, so one giant
    query cannot wipe the working set for every other caller. `run_cached` separately refuses to
    store such an entry at all; this is the half that protects everyone else.
    """
    total = sum(size for _, size in cache.values()) + incoming_bytes
    while cache:
        over_count = len(cache) >= max_entries > 0
        over_bytes = max_bytes > 0 and total > max_bytes
        if not (over_count or over_bytes):
            break
        # The incoming entry alone exceeds the ceiling — evicting further frees nothing that helps,
        # and would cost every other caller their entries for a payload that still will not fit.
        if over_bytes and not over_count and incoming_bytes > max_bytes:
            break
        _, evicted_size = cache.pop(next(iter(cache)))
        total -= evicted_size
