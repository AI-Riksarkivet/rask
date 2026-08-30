"""Behavioral tests for the demo data-peek router (§7) — real Lance reads, no S3/AGE.

``_read_dataset`` / ``_read_lineage_jsonb`` do the actual work (the route just maps the
medallion layout over them); lance reads a local path exactly like ``s3://``, so the
per-version schema walk, the row/version counts, and gold's embedded JSONB round-trip are
all provable infra-free. Playwright mocks this router empty — this is its only behavioral net.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from lineage.api.v1.endpoints import demo
from lineage.api.v1.endpoints.demo import PeekCache, _read_dataset, _read_lineage_jsonb


_K = 50  # default version cap — high enough to be inert for these small fixtures


@pytest.fixture
def cache() -> PeekCache:
    """A fresh per-test peek cache — app-scoped state (F-LIN-15), so isolation is construction, not a reset seam."""
    return PeekCache()


def test_read_dataset_absent_is_exists_false(tmp_path: Path, cache: PeekCache) -> None:
    result = _read_dataset(cache, "bronze$events", str(tmp_path / "missing.lance"), {}, _K)
    assert result.exists is False
    assert result.current_version is None and result.versions == []


def test_read_dataset_walks_schema_per_version(tmp_path: Path, cache: PeekCache) -> None:
    """The demo peek shows WHAT CHANGED in storage: each Lance version carries its own schema,
    so a column added in v2 appears only from v2 on — plus the live row count and version head."""
    uri = str(tmp_path / "features.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2], pa.int64())}), uri)
    ds = lance.dataset(uri)
    ds.add_columns({"score": "cast(id * 2 as double)"})  # schema evolution → v2

    result = _read_dataset(cache, "silver$features", uri, {}, _K)

    assert result.exists is True
    assert result.current_version == 2
    assert result.row_count == 2
    by_version = {v.version: [f.name for f in v.fields] for v in result.versions}
    assert by_version[1] == ["id"]
    assert by_version[2] == ["id", "score"]
    types = {f.name: f.type for v in result.versions for f in v.fields}
    assert types == {"id": "int64", "score": "double"}  # type_label passthrough for plain types
    assert result.lineage_jsonb is None  # only gold carries the embedded lineage


def test_read_dataset_gold_embeds_lineage_jsonb(tmp_path: Path, cache: PeekCache) -> None:
    """gold$catalog rows embed their lineage as a JSON string column — the peek parses it back."""
    uri = str(tmp_path / "catalog.lance")
    lineage_doc = {"upstream": ["silver$features"], "run": "tok123"}
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64()), "lineage": pa.array([json.dumps(lineage_doc)])}),
        uri,
    )

    result = _read_dataset(cache, "gold$catalog", uri, {}, _K)

    assert result.lineage_jsonb == lineage_doc


def test_read_lineage_jsonb_degrades_to_none() -> None:
    """Column absent, empty, or non-JSON → None, never a 500 (demo peek is best-effort)."""

    class _NoColumn:
        def to_table(self, columns: list[str]) -> pa.Table:
            raise KeyError(columns[0])

    assert _read_lineage_jsonb(_NoColumn()) is None

    class _Empty:
        def to_table(self, columns: list[str]) -> pa.Table:
            return pa.table({"lineage": pa.array([], pa.string())})

    assert _read_lineage_jsonb(_Empty()) is None

    class _Garbage:
        def to_table(self, columns: list[str]) -> pa.Table:
            return pa.table({"lineage": pa.array(["not json"])})

    assert _read_lineage_jsonb(_Garbage()) is None


# --------------------------------------------------------------------------- #
# §2 perf (2026-07-11): version-keyed cache — the 2s poll must not re-open every version
# --------------------------------------------------------------------------- #


def _counting_dataset(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap lance.dataset with a call counter (the S3 open count the cache must bound)."""
    opens = [0]
    real = lance.dataset

    def counted(*args: Any, **kwargs: Any) -> Any:
        opens[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(demo.lance, "dataset", counted)
    return opens


def test_peek_unchanged_tick_costs_one_probe_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache: PeekCache) -> None:
    # Cold tick: 1 probe + 1 per version. Unchanged tick: EXACTLY the probe — zero per-version
    # opens (immutable versions ⇒ cached entries are permanently valid, payload reused wholesale).
    uri = str(tmp_path / "features.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2], pa.int64())}), uri)
    lance.dataset(uri).add_columns({"score": "cast(id * 2 as double)"})  # → v2

    opens = _counting_dataset(monkeypatch)
    first = _read_dataset(cache, "silver$features", uri, {}, _K)
    assert first.current_version == 2 and opens[0] == 3  # probe + v1 + v2

    opens[0] = 0
    second = _read_dataset(cache, "silver$features", uri, {}, _K)
    assert opens[0] == 1  # the probe IS the change check — nothing else touched storage
    assert second == first

    # a NEW version re-reads ONLY itself (v1/v2 entries stay cached)
    lance.dataset(uri).add_columns({"flag": "cast(id > 1 as boolean)"})  # → v3
    opens[0] = 0
    third = _read_dataset(cache, "silver$features", uri, {}, _K)
    assert third.current_version == 3 and opens[0] == 2  # probe + v3 only
    assert [v.version for v in third.versions] == [1, 2, 3]


def test_peek_recreated_dataset_drops_the_old_incarnation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache: PeekCache) -> None:
    # A demo reset deletes + recreates a dataset at the SAME uri: the version numbers restart, so
    # cached per-version entries belong to the dead incarnation and must not leak into the new one.
    uri = str(tmp_path / "events.lance")
    lance.write_dataset(pa.table({"old_a": pa.array([1], pa.int64())}), uri)
    lance.dataset(uri).add_columns({"old_b": "cast(old_a as double)"})  # old incarnation → v2
    assert _read_dataset(cache, "bronze$events", uri, {}, _K).current_version == 2

    import shutil

    shutil.rmtree(uri)
    lance.write_dataset(pa.table({"fresh": pa.array([7], pa.int64())}), uri)  # new incarnation, v1

    result = _read_dataset(cache, "bronze$events", uri, {}, _K)
    assert result.current_version == 1
    assert [f.name for v in result.versions for f in v.fields] == ["fresh"]  # nothing stale


def test_peek_recreated_dataset_at_the_same_version_count_is_not_served_stale(
    tmp_path: Path,
    cache: PeekCache,
) -> None:
    # Review 2026-07-11 (the nastier incarnation case): the recreate reaches the SAME version count
    # as the cached incarnation while nobody polls — a bare version-number key would serve the dead
    # payload forever. Incarnation identity = the manifest TIMESTAMP, so the cache must rebuild.
    uri = str(tmp_path / "events.lance")
    lance.write_dataset(pa.table({"old": pa.array([1], pa.int64())}), uri)
    assert [f.name for v in _read_dataset(cache, "b$e", uri, {}, _K).versions for f in v.fields] == ["old"]

    import shutil

    shutil.rmtree(uri)
    lance.write_dataset(pa.table({"fresh": pa.array([7], pa.int64())}), uri)  # ALSO version 1

    result = _read_dataset(cache, "b$e", uri, {}, _K)
    assert result.current_version == 1
    assert [f.name for v in result.versions for f in v.fields] == ["fresh"]  # not the dead "old"


def test_peek_prunes_entries_below_the_window_floor(tmp_path: Path, cache: PeekCache) -> None:
    # Review 2026-07-11: the cascade mints a version per tick indefinitely — entries that scroll
    # out of the newest-K window must be evicted or the cache is a slow unbounded leak.
    uri = str(tmp_path / "leaky.lance")
    lance.write_dataset(pa.table({"a": pa.array([1], pa.int64())}), uri)
    lance.dataset(uri).add_columns({"b": "cast(a as double)"})
    _read_dataset(cache, "s$f", uri, {}, 2)  # caches v1, v2? no — window is [1,2], both kept
    lance.dataset(uri).add_columns({"c": "cast(a as double)"})  # → v3, window becomes [2,3]
    _read_dataset(cache, "s$f", uri, {}, 2)

    assert sorted(cache.version_fields[uri]) == [2, 3]  # v1 evicted — below the window floor


def test_peek_caps_to_the_newest_k_versions(tmp_path: Path, cache: PeekCache) -> None:
    # The cap bounds the COLD tick too: only the newest K versions are walked or reported.
    uri = str(tmp_path / "capped.lance")
    lance.write_dataset(pa.table({"a": pa.array([1], pa.int64())}), uri)
    lance.dataset(uri).add_columns({"b": "cast(a as double)"})
    lance.dataset(uri).add_columns({"c": "cast(a as double)"})  # → v3

    result = _read_dataset(cache, "silver$features", uri, {}, 2)
    assert [v.version for v in result.versions] == [2, 3]  # newest 2 only
    assert result.current_version == 3
