"""The storage walk and the orphan scan must DEGRADE, like the store categories beside them (MAINT-05).

The reconciler's own docstring states the rule: "a category whose source is unreachable reports UNAVAILABLE
**with the reason** while the other six still report. A drift report that 500s on one bad store tells
you nothing about the other six." Storage calls on the walk and in the scan stood outside every guard —
the walk's filesystem construction and the layout gate's own object probe — so one transient S3 failure
raised out of `reconcile()` and discarded a report whose store categories had already completed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from pydantic import SecretStr

from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import orphans as orphans_mod
from maintenance.services import reconcile as mod


class _NoBuckets:
    """The S3 read half — enough for `orphan_buckets` to be CHECKED, so its survival is provable."""

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": []}


def _run(tmp_path: Path) -> mod.ReconcileReport:
    settings = MaintenanceSettings(
        s3_bucket="lance-catalog",
        # BOTH halves. The key used to default to the RustFS tenant root, so this fixture got a working
        # credential for free; it no longer does, and an empty key fails inside object_store before the
        # injected fault this module is actually about ever fires.
        s3_access_key_id="unit",
        s3_secret_access_key=SecretStr("unit"),
    )
    (tmp_path / "control").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        mod.reconcile(
            settings,
            None,
            warehouses_enabled=False,
            control_root=f"file://{tmp_path / 'control'}",
            namespace_root=f"file://{tmp_path / 'data'}",
            bucket_client=_NoBuckets(),
            walk=True,
        )
    )


def test_a_filesystem_that_cannot_be_built_degrades_the_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = mod.fs_and_base

    def _boom(uri: str, storage_options: StorageOptions) -> tuple[pafs.FileSystem, str]:
        # ONLY the walk's own s3:// construction — the registry readers share this helper, and
        # failing those too would prove nothing about the categories under test.
        if uri.startswith("s3://"):
            raise OSError("endpoint refused the connection")
        return real(uri, storage_options)

    monkeypatch.setattr(mod, "fs_and_base", _boom)

    report = _run(tmp_path)

    assert {"unregistered_datasets", "absent_datasets"}.isdisjoint(report.counts), "a category whose walk blew up must not report a count"
    assert {"unregistered_datasets", "absent_datasets"} <= {u.category for u in report.unavailable}
    assert report.counts["orphan_buckets"] == 0, "the store categories that already completed must survive"
    assert [(note.source, "endpoint refused the connection" in note.reason) for note in report.incomplete] == [("storage:lance-catalog", True)]


def test_the_layout_gate_fails_closed_when_its_presence_probe_raises(tmp_path: Path) -> None:
    """The third site (orphans.py). The `tree/` and `_mem_wal/` probes already fail closed; the
    base_paths probe beside them raised straight out of the scan, which is how one dataset's transient
    read error became the whole tick's 500."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1, 2]}), uri)

    class _RaisesOnFiles(pafs.LocalFileSystem):
        """Fails the base-path PRESENCE probe only — the two directory probes and the listing still work.

        Written against the question, not the call shape: the probe is issued as one batched list since
        MAINT-12, and a fake keyed on the single-path form would silently stop exercising this guard.
        """

        def get_file_info(self, paths: Any) -> Any:  # noqa: ANN401 — pyarrow's own union (str | FileSelector | list)
            if isinstance(paths, list) or (isinstance(paths, str) and paths.endswith(".lance")):
                raise OSError("HeadObject: connection reset")
            return super().get_file_info(paths)

    scan = orphans_mod.scan_dataset(cast(pafs.FileSystem, _RaisesOnFiles()), uri, prefix=uri)
    assert scan.checked is False, "an undeterminable layout must read as unchecked, not raise"
    assert scan.orphans == []
    assert "connection reset" in (scan.reason or "")
