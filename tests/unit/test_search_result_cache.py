"""The search result cache evicts by BYTES as well as entry count (#141).

256 entries was a count, not a memory bound: a hit row carries display.body (transcript text), so a
full page of hits runs to megabytes and the cache could hold ~30× the pod's headroom while staying
"under" its limit. Eviction now honours a byte ceiling; the entry count remains the lookup bound.
"""

from __future__ import annotations

from typing import Any, cast

from search.services.result_cache import entry_bytes, run_cached


def _fake_key(monkeypatch: Any, name: str) -> None:
    from search.services import result_cache

    monkeypatch.setattr(result_cache, "cache_key", lambda *_a, **_k: ("ds", "sig", name))


def _run(cache: dict[Any, Any], rows: list[dict[str, Any]], name: str, monkeypatch: Any, *, max_size: int = 10, max_bytes: int = 0) -> list[dict[str, Any]]:
    _fake_key(monkeypatch, name)
    return run_cached(cache, max_size, max_bytes, cast(Any, None), cast(Any, None), None, None, lambda: rows)


def test_byte_ceiling_evicts_oldest(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    big = [{"body": "x" * 100}]
    budget = 3 * entry_bytes(big)  # room for three entries, not four
    for name in ("a", "b", "c", "d"):
        _run(cache, big, name, monkeypatch, max_bytes=budget)
    keys = [k[2] for k in cache]
    assert "a" not in keys  # oldest paid for the fourth
    assert keys[-1] == "d"
    assert sum(size for _, size in cache.values()) <= budget


def test_oversized_entry_is_served_but_never_cached(monkeypatch: Any) -> None:
    """One entry bigger than the whole budget must not wipe the cache to store itself."""
    cache: dict[Any, Any] = {}
    _run(cache, [{"body": "small"}], "small", monkeypatch, max_bytes=10_000)
    huge = [{"body": "x" * 20_000}]
    out = _run(cache, huge, "huge", monkeypatch, max_bytes=10_000)
    assert out == huge  # still answered
    assert [k[2] for k in cache] == ["small"]  # cache untouched


def test_zero_byte_bound_keeps_count_only_behaviour(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    for name in ("a", "b", "c"):
        _run(cache, [{"body": "x" * 10_000}], name, monkeypatch, max_size=2, max_bytes=0)
    assert [k[2] for k in cache] == ["b", "c"]  # count bound still enforced


def test_hit_returns_rows_and_refreshes_recency(monkeypatch: Any) -> None:
    cache: dict[Any, Any] = {}
    rows = [{"body": "kept"}]
    _run(cache, rows, "a", monkeypatch)
    _run(cache, [{"body": "other"}], "b", monkeypatch)
    hit = _run(cache, [{"body": "NOT recomputed"}], "a", monkeypatch)
    assert hit == rows  # served from cache, produce() ignored
    assert [k[2] for k in cache] == ["b", "a"]  # a moved to newest
