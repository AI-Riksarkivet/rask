"""The storage walk must DEGRADE, like the store categories beside it (MAINT-05).

The reconciler's own docstring states the rule: "a category whose source is unreachable reports UNAVAILABLE
**with the reason** while the other six still report. A drift report that 500s on one bad store tells
you nothing about the other six." The walk's filesystem construction is a storage call like any other, so one
transient S3 failure there reports its categories unavailable and keeps the store categories that completed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pyarrow.fs as pafs
import pytest
from pydantic import SecretStr

from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
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
