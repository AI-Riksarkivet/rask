"""#75 on-demand GC — preview must honour the current version, tag pins, the retain-window, and the age
cutoff, and never mutate; run passes the bounds through with the sweep's tag exemption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from catalog.services import maintenance
from lakehouse.storage_options import StorageOptions


class _Index:
    """``ds.tags`` / ``ds.branches`` — both answer a root-scoped ``{name: metadata}`` map."""

    def __init__(self, entries: dict[str, dict[str, int | str | None]]) -> None:
        self._entries = entries

    def list(self) -> dict[str, dict[str, int | str | None]]:
        return self._entries


class _Manifest:
    """Manifest blob for the #121 feature gate — proto3 varints at fields 9/10, empty = no flags."""

    def __init__(self, reader: int = 0, writer: int = 0) -> None:
        blob = b""
        if reader:
            blob += bytes([9 << 3]) + _varint(reader)
        if writer:
            blob += bytes([10 << 3]) + _varint(writer)
        self._blob = blob

    def serialized_manifest(self) -> bytes:
        return self._blob


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


class _FakeDs:
    def __init__(
        self,
        version: int,
        versions: list[dict[str, Any]],
        tags: dict[str, dict[str, int | str | None]],
        stats: Any = None,
        feature_flags: tuple[int, int] = (0, 0),
    ) -> None:
        self.version = version
        self._versions = versions
        self.tags = _Index(tags)
        self.branches = _Index({})
        self._stats = stats
        self.cleaned: dict[str, Any] | None = None
        self.optimize = _Optimize()
        self._ds = _Manifest(*feature_flags)

    def versions(self) -> list[dict[str, Any]]:
        return self._versions

    def get_fragments(self) -> list[Any]:
        """The fragment walk the compaction gate performs — reached only by a dataset that BOTH sets
        flag 16 and can say where it lives, which no fake here does, so an empty list is faithful.
        Present because `compact_now` now STATES that surface (CAT-CORE-15) instead of taking `Any`."""
        return []

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


def test_run_gc_passes_bounds_and_reports_stats() -> None:
    ds = _FakeDs(version=3, versions=_versions(3), tags={}, stats=SimpleNamespace(old_versions=2, bytes_removed=4096))
    out = maintenance.run_gc(ds, retention_days=7, retain_versions=1)
    assert out == {"ok": True, "old_versions_removed": 2, "bytes_removed": 4096}
    # tagged versions are exempt exactly like the sweep, and the retain bound is forwarded
    assert ds.cleaned is not None
    assert ds.cleaned["error_if_tagged_old_versions"] is False
    assert ds.cleaned["retain_versions"] == 1


def test_the_flag_16_refusal_is_FAIL_CLOSED_when_the_bases_cannot_be_read() -> None:
    """The compaction gate `version/create` asks of a Rewrite weighs EVIDENCE about the bases, and this
    dataset offers none: it sets flag 16 and cannot say where it lives, so no base can be probed. Unknown
    resolves to refusal."""
    from lance_namespace import UnsupportedOperationError

    ds = _FakeDs(version=1, versions=[], tags={}, feature_flags=(16, 16))
    with pytest.raises(UnsupportedOperationError, match="base_paths .shallow clone"):
        maintenance.require_compactable(ds, table_location="", storage_options=StorageOptions())


def test_run_gc_REFUSES_an_unknown_flag() -> None:
    """Flag 64 (data overlays) is unknown to every gate here — the root-scoped allowance covers
    `base_paths` and nothing else."""
    from lance_namespace import UnsupportedOperationError

    ds = _FakeDs(version=1, versions=[], tags={}, feature_flags=(64, 64))
    with pytest.raises(UnsupportedOperationError, match="data overlays"):
        maintenance.run_gc(ds, retention_days=7, retain_versions=2)
    assert ds.cleaned is None, "the delete ran anyway — the refusal came too late"


# ---------------------------------------------------------------- the on-demand doors and the SHALLOW-CLONE SOURCE


def _rows(n: int = 20) -> Any:
    import pyarrow as pa

    return pa.table({"id": pa.array(range(n), pa.int64())})


def test_run_gc_PERMITS_a_clone_the_SWEEP_reclaims_on_EVERY_TICK(tmp_path: Any) -> None:
    """The other half of row 13, and the reason the divergence was closed rather than documented as
    deliberate: version reclamation is ROOT-SCOPED, the sweep's gate (`describe_gc_unsupported_flags`)
    tolerates `base_paths`, and the cron runs it unattended against this same dataset every tick. A
    button that refuses what the timer performs anyway prevents no rewrite — it only denies the
    operator the remedy, while telling them the cron agrees.

    Measured on pylance 9.0.0 (`SUPPORTED_FOR_GC`): one `cleanup_old_versions` on a clone removed the
    2 clone-owned files and left all 4 base-owned ones, across six cleanup shapes and ten cycles.
    Asserted here rather than assumed: the SOURCE's files must survive the reclaim.
    """
    import lance

    from lakehouse.features import describe_gc_unsupported_flags, manifest_feature_flags

    source = str(tmp_path / "src.lance")
    src = lance.write_dataset(_rows(), source)
    clone = str(tmp_path / "clone.lance")
    src.shallow_clone(clone, reference=src.version)
    ds = lance.dataset(clone)
    assert manifest_feature_flags(ds)[1] & 16, "fixture assumption: a clone sets flag 16"
    assert describe_gc_unsupported_flags(*manifest_feature_flags(ds)) is None, "premise gone: the CRON refuses this clone's GC too"
    source_files = sorted(p.name for p in (tmp_path / "src.lance" / "data").iterdir())

    result = maintenance.run_gc(ds, retention_days=0, retain_versions=1)

    assert result["ok"] is True
    assert sorted(p.name for p in (tmp_path / "src.lance" / "data").iterdir()) == source_files, "the reclaim deleted the SOURCE's files"
    assert len(lance.dataset(clone).to_table()) == 20, "the clone stopped resolving its rows after the reclaim"
