"""Two twin caches on one AppState; only one had a memory ceiling.

`points_cache` memoizes the full Arrow IPC `/points` payload — coordinates, codes and keys for every
projected row of a corpus — and evicted on ENTRY COUNT alone: `while len(cache) >= 12`. Its own
comment at the constant says "each is multi-MB" and never turns that into a byte bound.

The arithmetic is the finding. `available_modes` derives one space per declared embedding key, so
`semantic`/`visual`/`scene` across four corpora is already 12 distinct keys, each projecting a few
million rows. At ~100 MB per payload that is 1.2 GB resident in a pod the chart gives `replicas: 1` —
the viewer OOM-kills and every explorer route dies with it.

The identical cache one field over — `search_cache`, same `AppState` — already carries
`search_cache_bytes = 64 MiB` alongside its count bound, because someone measured this. The fix was
applied to one of the two twins.

SO THE BOUND IS EXTRACTED RATHER THAN COPIED A SECOND TIME. That is the actual defect: two caches
with the same invalidation model and the same eviction problem, fixed independently, one forgotten.
A shared `evict_to_bounds` means the next cache added to AppState inherits both bounds instead of
re-litigating them.

The atlas entry is `bytes`, so its size is EXACT — `len(payload)` — where the search cache has to
approximate. That is a reason to share the eviction, not to keep them apart.
"""

from __future__ import annotations

from service_kit.media.cache_bounds import evict_to_bounds


def test_an_entry_larger_than_the_whole_ceiling_does_not_empty_the_cache() -> None:
    """The pathological case: one oversized payload must not evict everything and then not fit.

    `run_cached` already refuses to STORE such an entry; the eviction helper must not clear the cache
    on its behalf first, or a single giant query wipes the working set for everyone else.
    """
    cache: dict[str, tuple[bytes, int]] = {"keep": (b"x", 100)}
    evict_to_bounds(cache, max_entries=100, max_bytes=1000, incoming_bytes=5000)
    assert "keep" in cache, "an unstorable oversized entry emptied the cache on its way to not fitting"
