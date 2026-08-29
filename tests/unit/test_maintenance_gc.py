"""#75 on-demand GC — preview must honour the current version, tag pins, the retain-window, and the age
cutoff, and never mutate; run passes the bounds through with the sweep's tag exemption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from catalog.services import maintenance


class _Tags:
    def __init__(self, tags: dict[str, dict[str, int]]) -> None:
        self._tags = tags

    def list(self) -> dict[str, dict[str, int]]:
        return self._tags


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
        tags: dict[str, dict[str, int]],
        stats: Any = None,
        feature_flags: tuple[int, int] = (0, 0),
    ) -> None:
        self.version = version
        self._versions = versions
        self.tags = _Tags(tags)
        self._stats = stats
        self.cleaned: dict[str, Any] | None = None
        self.optimize = _Optimize()
        self._ds = _Manifest(*feature_flags)

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
    assert ds.optimize.compact_kw == {"target_rows_per_fragment": 1_000_000, "batch_size": 64, "num_threads": 2}
    assert ds.optimize.indices_optimized is True  # indices kept covering the new fragments


def test_compact_now_omits_target_when_unset() -> None:
    ds = _FakeDs(version=1, versions=_versions(1), tags={})
    maintenance.compact_now(ds, target_rows_per_fragment=None)
    # None → Lance's default FRAGMENT sizing; the #93 memory floor is forced regardless — rows are
    # not a unit of memory, and this door runs on the catalog pod.
    assert ds.optimize.compact_kw == {"batch_size": 64, "num_threads": 2}


# ---------------------------------------------------------------- #121 the on-demand doors REFUSE what the sweep refuses


def test_compact_now_REFUSES_a_shallow_clone_before_any_rewrite() -> None:
    """The sweep got this gate at #64; these doors are the SAME operations wired to a UI button and
    shipped with no gate at all. Measured then: compacting a flag-16 clone reported success and
    silently materialised a full local copy of data it had only referenced."""
    from lance_namespace import UnsupportedOperationError

    ds = _FakeDs(version=1, versions=[], tags={}, feature_flags=(16, 16))
    with pytest.raises(UnsupportedOperationError, match="base_paths .shallow clone"):
        maintenance.compact_now(ds, target_rows_per_fragment=None)
    assert ds.optimize.compact_kw is None, "the rewrite ran anyway — the refusal came too late"


def test_run_gc_REFUSES_the_same_flags() -> None:
    from lance_namespace import UnsupportedOperationError

    ds = _FakeDs(version=1, versions=[], tags={}, feature_flags=(64, 64))
    with pytest.raises(UnsupportedOperationError, match="data overlays"):
        maintenance.run_gc(ds, retention_days=7, retain_versions=2)
    assert ds.cleaned is None, "the delete ran anyway — the refusal came too late"


def test_a_plain_dataset_still_compacts_with_the_93_memory_floor() -> None:
    """The gate must not refuse healthy datasets — and the compact now carries #93's bounds, because
    the default batch size read ~15 GB/thread on a blob tier and this door runs on the CATALOG pod."""
    ds = _FakeDs(version=1, versions=[], tags={})
    result = maintenance.compact_now(ds, target_rows_per_fragment=None)
    assert result["ok"] is True
    assert ds.optimize.compact_kw is not None
    assert ds.optimize.compact_kw["batch_size"] == 64
    assert ds.optimize.compact_kw["num_threads"] == 2


# ---------------------------------------------------------------- the on-demand doors and the SHALLOW-CLONE SOURCE


class _LocatedDs(_FakeDs):
    """A `_FakeDs` that knows where it lives — `lance.LanceDataset.uri`, which the real doors hold."""

    def __init__(self, uri: str, **kw: Any) -> None:
        super().__init__(version=1, versions=[], tags={}, **kw)
        self.uri = uri


def test_compact_now_REFUSES_a_dataset_another_one_RESOLVES_ITS_FILES_THROUGH() -> None:
    """#114 one click away. The sweep got this guard; the UI button running the same three verbs did not.

    The dataset in danger carries NO flag of its own: flag 16 marks the CLONE, while the SOURCE looks
    completely ordinary (measured: source `(0, 0)` with no base_paths, clone `(16, 16)` naming the
    source). So the per-dataset feature check `require_maintainable` performs cannot see it, and this
    door had nothing else — one operator clicking "compact now" on the source rewrites the files the
    clone is the only other reference to, and the clone then fails to open in a fresh process.
    """
    from lance_namespace import UnsupportedOperationError

    from service_kit.lakehouse.base_refs import BaseRefs

    ds = _LocatedDs("s3://wh/src.lance")
    protected = BaseRefs(protected={"wh/src.lance"})

    with pytest.raises(UnsupportedOperationError, match="resolves its files through"):
        maintenance.compact_now(ds, target_rows_per_fragment=None, protected=protected)
    assert ds.optimize.compact_kw is None, "the rewrite ran anyway — the refusal came too late"


def test_run_gc_REFUSES_the_same_source() -> None:
    """Cleanup is the step that actually deletes: measured, `compact_files` ADDS the merged file
    (4 -> 5, the clone still opens) and `cleanup_old_versions` then removes the obsoleted originals
    (-> 1) after which the clone will not open. A guard on compaction alone is walked straight past."""
    from lance_namespace import UnsupportedOperationError

    from service_kit.lakehouse.base_refs import BaseRefs

    ds = _LocatedDs("s3://wh/src.lance")

    with pytest.raises(UnsupportedOperationError, match="resolves its files through"):
        maintenance.run_gc(ds, retention_days=7, retain_versions=2, protected=BaseRefs(protected={"wh/src.lance"}))
    assert ds.cleaned is None, "the delete ran anyway — the refusal came too late"


def test_an_UNPROTECTED_dataset_still_passes_the_base_ref_guard() -> None:
    """The negative that makes the two refusals above mean something: a guard that refused everything
    would satisfy them both."""
    from service_kit.lakehouse.base_refs import BaseRefs

    ds = _LocatedDs("s3://wh/other.lance")
    result = maintenance.compact_now(ds, target_rows_per_fragment=None, protected=BaseRefs(protected={"wh/src.lance"}))
    assert result["ok"] is True


def test_a_protected_map_with_NO_LOCATION_to_check_it_against_refuses() -> None:
    """ "We could not tell" reads as the refusal, the same direction every other gate here runs in.
    A caller that collected the estate's base references and then hands over a dataset whose location
    cannot be read has not shown that this dataset is safe — and the failure this guards is
    irreversible deletion of another dataset's only copy."""
    from lance_namespace import UnsupportedOperationError

    from service_kit.lakehouse.base_refs import BaseRefs

    ds = _FakeDs(version=1, versions=[], tags={})  # no `uri`
    with pytest.raises(UnsupportedOperationError, match="location"):
        maintenance.compact_now(ds, target_rows_per_fragment=None, protected=BaseRefs(protected={"wh/src.lance"}))


# ---------------------------------------------------------------- a pyo3 PANIC is not an Exception


class _Panic(BaseException):
    """Stands in for `pyo3_runtime.PanicException`, which derives from BaseException — not Exception."""


class _RaisingOptimize(_Optimize):
    """`ds.optimize` whose index step raises the given BaseException after a successful compaction."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def optimize_indices(self) -> None:
        raise self._error


def test_compact_now_survives_a_PANICKING_index_report() -> None:
    """`contextlib.suppress(Exception)` cannot catch a Rust panic, and `index_stats` panics for real:
    an unimplemented arm (`lance-index/src/scalar/json.rs`) on a JSON index. Measured on the sweep,
    where it answered the whole run HTTP 500 — which is why `compact_one` and `inspect_indices` each
    guard with `except BaseException` and re-raise only KeyboardInterrupt/SystemExit.

    This door had the weaker guard, so the same panic would have thrown away a compaction that had
    ALREADY happened and reported the button as failed. Index work is best-effort here by design; the
    compaction it follows is not.
    """
    ds = _FakeDs(version=1, versions=[], tags={})
    ds.optimize = _RaisingOptimize(_Panic("not yet implemented"))

    result = maintenance.compact_now(ds, target_rows_per_fragment=None)

    assert result == {"ok": True, "fragments_removed": 4, "fragments_added": 1}


def test_compact_now_still_lets_a_KEYBOARD_INTERRUPT_through() -> None:
    """The two BaseExceptions that must never be swallowed — a caught SystemExit turns a shutdown into
    a hang, and the sweep's own guards make the same exception."""
    ds = _FakeDs(version=1, versions=[], tags={})
    ds.optimize = _RaisingOptimize(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        maintenance.compact_now(ds, target_rows_per_fragment=None)


def test_sibling_base_refs_FINDS_a_real_clone_reference(tmp_path: Any) -> None:
    """The enumeration, against real Lance datasets — because a guard wired to an empty map is
    indistinguishable from no guard at all.

    That is not hypothetical here: `protected_roots` shipped with `storage_options` accepted and
    DROPPED, so every `s3://` open failed, `protected` came back empty on every tick, and both guards
    built on it were inert against the exact data-loss path they exist to close. So this asserts the
    map is NON-EMPTY and names the source, not merely that a call was made.
    """
    import lance
    import pyarrow as pa
    from catalog.services import maintenance as svc

    source = str(tmp_path / "aa11_ns$src.lance")
    src = lance.write_dataset(pa.table({"id": pa.array(range(9), pa.int64())}), source)
    src.shallow_clone(str(tmp_path / "bb22_ns$clone.lance"), reference=src.version)

    refs = svc.sibling_base_refs(source, {})

    assert refs.is_protected(source) is not None, f"the clone's reference to its source was not found: {refs}"
    assert refs.is_protected(str(tmp_path / "cc33_ns$unrelated.lance")) is None
