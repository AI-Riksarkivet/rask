"""Version reclamation has one owner, maintenance's pass, and no writer's commit deletes a version ([[LH-245]]).

Lance's automatic cleanup runs INSIDE the commit of whoever writes, every N commits, from the
`lance.auto_cleanup.*` manifest keys (`lance_docs/lance/guide/read_and_write.md:610-671`). Measured on pylance 12.0.0: with
`interval=1, older_than=0s`, one ordinary append took a table from versions 1..7 to 7..8. That deletion
passes none of maintenance's gates (a legal hold among them), runs under the writer's identity, and is recorded nowhere,
so a pass removes every key before any gate can return.

Driven on real pylance through one dataset's pass, `table_pass.maintain_dataset`. The pass is the backstop for a table
nobody commits on; a table that is committed on is disarmed by the committing door first
(`tests/integration/test_an_ordinary_append_never_deletes_a_version.py`). A held table, whose policy keeps its whole
history, is the exception: the catalog vends it no maintain tier ([[XC-084]]), so the pass writes nothing there, its
disarm included, and its keys wait for a door's commit, which disarms first.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan


CATALOG = "http://catalog.test"
#: The keys an armed table carries. `retain_versions` included, because pylance's own
#: `disable_auto_cleanup` deletes only the other two and leaves it standing (measured on 12.0.0).
_ARMED: dict[str, str | None] = {"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s", "lance.auto_cleanup.retain_versions": "1"}


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


def _armed_table(tmp_path: Path) -> str:
    uri = str(tmp_path / "t.lance")
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(table, uri, mode="append")
    lance.dataset(uri).update_config(_ARMED)
    return uri


def _auto_cleanup_keys(uri: str) -> dict[str, str]:
    return {k: v for k, v in lance.dataset(uri).config().items() if k.startswith("lance.auto_cleanup.")}


def _vend_covering(uri: str) -> httpx.Response:
    """The catalog's maintain-tier vend for the table at ``uri``: the disarm is a commit, signed by it alone ([[LH-219]])."""
    vended = {"aws_access_key_id": "VENDED", "aws_secret_access_key": "vended-secret"}
    return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": vended}, "location": uri})


def test_a_pass_disarms_an_armed_table_under_its_vend(tmp_path: Path, maintain_one: Callable[..., DatasetResult]) -> None:
    """The keys' removal is a commit, so it is signed by the table's maintain-tier vend alone ([[LH-219]])."""
    uri = _armed_table(tmp_path)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "unused", "catalog_url": CATALOG})

    with respx.mock(assert_all_called=False) as door:
        vend = door.post(f"{CATALOG}/management/v1/table/db$t/credentials", params={"tier": "maintain"}).mock(return_value=_vend_covering(uri))
        maintain_one(uri, settings, plan=DatasetPlan(older_than=timedelta(0)))

    assert (_auto_cleanup_keys(uri), vend.called) == ({}, True)


def test_a_held_table_keeps_its_keys_and_its_history(tmp_path: Path, maintain_one: Callable[..., DatasetResult]) -> None:
    """A held table asks for no vend and keeps its keys, and its history: the hold leaves the pass nothing to write."""
    uri = _armed_table(tmp_path)
    versions = {version["version"] for version in lance.dataset(uri).versions()}
    settings = MaintenanceSettings.model_validate({"s3_bucket": "unused", "catalog_url": CATALOG})

    with respx.mock(assert_all_called=False) as door:
        vend = door.post(f"{CATALOG}/management/v1/table/db$t/credentials", params={"tier": "maintain"}).mock(return_value=_vend_covering(uri))
        result = maintain_one(uri, settings, plan=DatasetPlan(older_than=timedelta(0), cleanup_enabled=False))

    assert (_auto_cleanup_keys(uri), vend.called, result.old_versions_removed) == (_ARMED, False, 0)
    assert versions <= {version["version"] for version in lance.dataset(uri).versions()}, "a version the held table kept is gone"
