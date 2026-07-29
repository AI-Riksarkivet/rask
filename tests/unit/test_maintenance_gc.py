"""#75 on-demand GC — preview must honour the current version, tag pins, the retain-window, and the age
cutoff, and never mutate; run passes the bounds through with the sweep's tag exemption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from catalog.services import maintenance


class _Tags:
    def __init__(self, tags: dict[str, dict[str, int]]) -> None:
        self._tags = tags

    def list(self) -> dict[str, dict[str, int]]:
        return self._tags


class _FakeDs:
    def __init__(
        self,
        version: int,
        versions: list[dict[str, Any]],
        tags: dict[str, dict[str, int]],
        stats: Any = None,
    ) -> None:
        self.version = version
        self._versions = versions
        self.tags = _Tags(tags)
        self._stats = stats
        self.cleaned: dict[str, Any] | None = None
        self.optimize = _Optimize()

    def versions(self) -> list[dict[str, Any]]:
        return self._versions

    def cleanup_old_versions(self, **kw: Any) -> Any:
        self.cleaned = kw  # record that a mutation was attempted (and with what bounds)
        return self._stats


class _Optimize:
    def __init__(self) -> None:
        self.compact_kw: dict[str, Any] | None = None
        self.indices_optimized = False

    def compact_files(self, **kw: Any) -> Any:
        self.compact_kw = kw
        return SimpleNamespace(fragments_removed=4, fragments_added=1)

    def optimize_indices(self) -> None:
        self.indices_optimized = True


def _versions(n: int, *, age_days: int = 100) -> list[dict[str, Any]]:
    old = datetime.now(UTC) - timedelta(days=age_days)
    return [{"version": v, "timestamp": old} for v in range(1, n + 1)]


def test_preview_retains_current_tags_and_recent() -> None:
    ds = _FakeDs(version=5, versions=_versions(5), tags={"blessed": {"version": 2}})
    out = maintenance.preview_gc(ds, retention_days=None, retain_versions=2)
    # keep 5 (current) + 4 (retain-2 window) + 2 (tag) → reclaim {3, 1}
    assert set(out["eligible_versions"]) == {1, 3}
    assert out["protected_tags"] == {"blessed": 2}
    assert out["current_version"] == 5
    assert ds.cleaned is None  # preview NEVER mutates


def test_preview_age_cutoff_spares_recent_versions() -> None:
    # v1-v3 are 100 days old (past a 1-day cutoff → eligible); v4 is fresh (spared); v5 is current.
    versions = _versions(3) + [
        {"version": 4, "timestamp": datetime.now(UTC) - timedelta(hours=1)},
        {"version": 5, "timestamp": datetime.now(UTC)},
    ]
    ds = _FakeDs(version=5, versions=versions, tags={})
    out = maintenance.preview_gc(ds, retention_days=1, retain_versions=None)
    assert set(out["eligible_versions"]) == {1, 2, 3}  # 4 too new, 5 current


def test_run_gc_passes_bounds_and_reports_stats() -> None:
    ds = _FakeDs(version=3, versions=_versions(3), tags={}, stats=SimpleNamespace(old_versions=2, bytes_removed=4096))
    out = maintenance.run_gc(ds, retention_days=7, retain_versions=1)
    assert out == {"ok": True, "old_versions_removed": 2, "bytes_removed": 4096}
    # tagged versions are exempt exactly like the sweep, and the retain bound is forwarded
    assert ds.cleaned is not None
    assert ds.cleaned["error_if_tagged_old_versions"] is False
    assert ds.cleaned["retain_versions"] == 1


def test_compact_now_passes_target_and_optimizes_indices() -> None:
    ds = _FakeDs(version=1, versions=_versions(1), tags={})
    out = maintenance.compact_now(ds, target_rows_per_fragment=1_000_000)
    assert out == {"ok": True, "fragments_removed": 4, "fragments_added": 1}
    assert ds.optimize.compact_kw == {"target_rows_per_fragment": 1_000_000}
    assert ds.optimize.indices_optimized is True  # indices kept covering the new fragments


def test_compact_now_omits_target_when_unset() -> None:
    ds = _FakeDs(version=1, versions=_versions(1), tags={})
    maintenance.compact_now(ds, target_rows_per_fragment=None)
    assert ds.optimize.compact_kw == {}  # None → Lance's default sizing (no kwarg forced)
