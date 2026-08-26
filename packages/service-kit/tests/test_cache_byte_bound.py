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


def test_eviction_honours_the_BYTE_ceiling_not_just_the_count() -> None:
    """The finding: twelve multi-MB entries is a count of twelve and a gigabyte of memory."""
    cache: dict[str, tuple[bytes, int]] = {}
    payload = b"x" * 1000

    for i in range(10):
        evict_to_bounds(cache, max_entries=100, max_bytes=3000, incoming_bytes=len(payload))
        cache[f"k{i}"] = (payload, len(payload))

    total = sum(size for _, size in cache.values())
    assert total <= 3000, f"cache holds {total} bytes against a 3000-byte ceiling"
    assert len(cache) <= 3


def test_the_count_bound_still_applies() -> None:
    """Both bounds, not one replacing the other — the count bound is the LOOKUP bound."""
    cache: dict[str, tuple[bytes, int]] = {}
    for i in range(10):
        evict_to_bounds(cache, max_entries=4, max_bytes=0, incoming_bytes=1)
        cache[f"k{i}"] = (b"x", 1)

    assert len(cache) <= 4


def test_eviction_is_oldest_first() -> None:
    """Insertion order is the LRU proxy both caches already relied on."""
    cache: dict[str, tuple[bytes, int]] = {}
    for i in range(3):
        cache[f"k{i}"] = (b"x", 1)

    evict_to_bounds(cache, max_entries=2, max_bytes=0, incoming_bytes=1)
    assert "k0" not in cache, "eviction did not drop the oldest entry"


def test_a_zero_byte_ceiling_disables_only_the_BYTE_bound() -> None:
    """`0 = off` is the convention `search_cache_bytes` already documents; keep it identical so the
    two twins cannot diverge on the meaning of their own settings."""
    cache: dict[str, tuple[bytes, int]] = {}
    for i in range(5):
        evict_to_bounds(cache, max_entries=100, max_bytes=0, incoming_bytes=10_000_000)
        cache[f"k{i}"] = (b"x", 10_000_000)

    assert len(cache) == 5, "a zero byte ceiling evicted on bytes anyway"


def test_an_entry_larger_than_the_whole_ceiling_does_not_empty_the_cache() -> None:
    """The pathological case: one oversized payload must not evict everything and then not fit.

    `run_cached` already refuses to STORE such an entry; the eviction helper must not clear the cache
    on its behalf first, or a single giant query wipes the working set for everyone else.
    """
    cache: dict[str, tuple[bytes, int]] = {"keep": (b"x", 100)}
    evict_to_bounds(cache, max_entries=100, max_bytes=1000, incoming_bytes=5000)
    assert "keep" in cache, "an unstorable oversized entry emptied the cache on its way to not fitting"
