"""The orphan category must DEGRADE, like the seven store categories beside it (MAINT-05).

The module's own docstring states the rule: "a category whose source is unreachable reports UNAVAILABLE
**with the reason** while the other six still report. A drift report that 500s on one bad store tells
you nothing about the other six." Three storage calls on the orphan path stood outside every guard —
the filesystem construction, the per-dataset scan aggregate, and the layout gate's own object probe —
so one transient S3 failure raised out of `reconcile()`, answered the cron tick 500, and discarded a
report whose seven store categories had already completed.
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
        s3_secret_access_key=SecretStr("unit"),
        orphan_scan_enabled=True,
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
        )
    )


def _assert_degraded(report: mod.ReconcileReport, needle: str) -> None:
    assert "orphan_files" not in report.counts, "a category whose scan blew up must not report a count"
    assert report.counts["orphan_buckets"] == 0, "the store categories that already completed must survive"
    assert any(needle in note.reason for note in report.incomplete), f"no incomplete-scan note naming {needle!r}: {[n.reason for n in report.incomplete]}"


def test_a_filesystem_that_cannot_be_built_degrades_the_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = mod.fs_and_base

    def _boom(uri: str, storage_options: dict[str, str]) -> tuple[pafs.FileSystem, str]:
        # ONLY the orphan scan's own s3:// construction — the registry readers share this helper, and
        # failing those too would prove nothing about the category under test.
        if uri.startswith("s3://"):
            raise OSError("endpoint refused the connection")
        return real(uri, storage_options)

    monkeypatch.setattr(mod, "fs_and_base", _boom)
    _assert_degraded(_run(tmp_path), "endpoint refused the connection")


def test_a_scan_that_raises_degrades_the_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> Any:
        raise OSError("the object store went away mid-scan")

    monkeypatch.setattr(mod, "scan_datasets", _boom)
    _assert_degraded(_run(tmp_path), "the object store went away mid-scan")


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
