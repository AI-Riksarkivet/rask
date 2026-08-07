"""Version-keyed search result cache — the read-fast tier for the search path.

Mirrors the atlas ``points_cache``: memoize a query's full result on
``(dataset, table versions, query hash)``. Any write to a table the query reads
advances that table's Lance version, so the key changes and the stale entry
becomes unreachable — Firn's invalidation model, no manual busting. The cache
lives on :class:`~service_kit.media.state.AppState` (per app instance, never
module-global) and is sized by ``settings.search_cache_size`` (0 = disabled).

It sits at the ROUTER layer (which owns AppState), so :func:`run_search` stays
pure. The design — key = (dataset, table-version-signature, query-hash) — is the
portable part: it transfers verbatim to whatever owns serving later (e.g. the
lance-ns catalog), independent of this Python implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

import lance


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from search.services.spec import SearchSpec
    from service_kit.lancekit.registry import DatasetHandle

logger = logging.getLogger(__name__)

#: The cache key: (dataset id, table-version signature, query hash).
CacheKey = tuple[str, str, str]


def _search_tables(handle: DatasetHandle) -> list[str]:
    """Every table a search could read — row table + fts table + vector-binding
    tables. A write to any of them must invalidate a cached result, so all of
    their versions go into the key."""
    search = handle.descriptor.declared.search
    if search is None:
        return []
    tables = {search.row_table}
    if search.fts is not None:
        tables.add(search.fts.table)
    for binding in search.vectors.values():
        tables.add(binding.table)
    return sorted(tables)


def version_signature(handle: DatasetHandle) -> str:
    """A ``table:version|...`` signature over every table a search reads.

    Reading a table's version is one cheap manifest read (a stat locally, one
    small object read over S3) — far cheaper than the query it guards, so paying
    it on every request (hit or miss) is a good trade. A table that can't be
    opened contributes ``?`` (it degrades to a stable-but-uninvalidatable key
    rather than crashing the request)."""
    parts: list[str] = []
    for table in _search_tables(handle):
        try:
            ds = lance.dataset(handle.table_uri(table), storage_options=handle.storage_options)
            parts.append(f"{table}:{ds.version}")
        except Exception:
            parts.append(f"{table}:?")
    return "|".join(parts)


def query_hash(spec: SearchSpec, filters: Mapping[str, str] | None, image_bytes: bytes | None) -> str:
    """A stable hash of EVERY result-affecting input. Miss one and a stale result
    is served, so the payload is exhaustive: the spec knobs, the sorted filters,
    and the uploaded image's own digest."""
    payload = {
        "mode": spec.mode,
        "q": spec.q,
        "q_vec": spec.q_vec,
        "n": spec.n,
        "where": spec.where,
        "rerank": spec.rerank,
        "rerank_n": spec.rerank_n,
        "weight": spec.weight,
        "fuzziness": spec.fuzziness,
        "phrase": spec.phrase,
        "prefilter": spec.prefilter,
        "filters": dict(sorted((filters or {}).items())),
        "image": hashlib.sha256(image_bytes).hexdigest() if image_bytes else None,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def cache_key(
    handle: DatasetHandle,
    spec: SearchSpec,
    filters: Mapping[str, str] | None,
    image_bytes: bytes | None,
) -> CacheKey:
    return (handle.id, version_signature(handle), query_hash(spec, filters, image_bytes))


def entry_bytes(result: list[dict[str, Any]]) -> int:
    """Approximate heap footprint as serialized-JSON length. Hit rows are plain
    str/num dicts (they leave the service as JSON anyway), so the serialized
    length tracks real size closely enough to budget on."""
    return len(json.dumps(result, default=str))


def run_cached(
    cache: dict[CacheKey, tuple[list[dict[str, Any]], int]],
    max_size: int,
    max_bytes: int,
    handle: DatasetHandle,
    spec: SearchSpec,
    filters: Mapping[str, str] | None,
    image_bytes: bytes | None,
    produce: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return the cached hits for this query, else compute via ``produce`` and
    memoize. Bypassed entirely when ``max_size <= 0`` (cache disabled), so the
    version reads are only paid when the cache is on. Eviction is LRU under TWO
    bounds: entry count (the lookup bound) and total bytes (the memory bound —
    a hit row carries transcript text, so 256 entries was a count, not a cap;
    #141). ``max_bytes <= 0`` disables the byte bound only. An entry larger than
    the whole byte budget is returned uncached — the cache must never hold a
    single object that busts its own ceiling."""
    if max_size <= 0:
        return produce()
    key = cache_key(handle, spec, filters, image_bytes)
    hit = cache.get(key)
    if hit is not None:
        cache[key] = cache.pop(key)  # move-to-end → LRU recency
        return hit[0]
    result = produce()
    nbytes = entry_bytes(result)
    if 0 < max_bytes < nbytes:
        return result
    total = sum(size for _, size in cache.values()) + nbytes
    while cache and (len(cache) >= max_size or (max_bytes > 0 and total > max_bytes)):
        _, freed = cache.pop(next(iter(cache)))  # evict oldest
        total -= freed
    cache[key] = (result, nbytes)
    return result
