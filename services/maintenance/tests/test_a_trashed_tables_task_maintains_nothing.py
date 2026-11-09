"""diff2 F6(d) — maintenance must not maintain a table that is IN THE TRASH.

A recoverable drop deregisters the table's ``__manifest`` row and files a trash record. It moves no byte: the dataset
directory keeps its ``_versions/`` child, and its location's claim stays held for the trashed table's uuid
([[LH-204]]). The consequence of maintaining it is not a wasted cycle, it is silent irreversible mutation of data the
owner was told is frozen:

    Karin drops `silver$readings_2019` on a Friday. Grace is 7 days, so the UI tells her she can undrop until next
    Friday. A pass over the weekend runs `cleanup_old_versions` on that dataset. Monday she undrops and gets a table
    back — but not the one she dropped: the version history she was trying to recover to has been pruned.

A table's task is keyed by its uuid, and so is its trash record ([[LH-525]]), so the pass asks the record by the uuid
before it opens anything (`maintenance.services.table_pass.run_table_pass`): the table and every branch of it, which are
passed only within their table's pass, are left alone. A passed deadline is permission for the PURGE to reclaim, never
for a pass to expire: `undrop` deliberately does not consult the clock, so a record past its deadline that nothing has
reclaimed still describes recoverable bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import lance
import pyarrow as pa
import pytest

from lakehouse import location_claims, maintenance_due, trash
from lakehouse.base_refs import normalise
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import table_pass
from maintenance.services.expiry import ExpiryPolicy
from maintenance.services.naming import TableIds
from maintenance.services.table_pass import TableTask, run_table_pass


def _dataset(path: Path) -> str:
    """A two-version Lance dataset, enough for expiry to have something to do."""
    uri = str(path)
    table = pa.table({"n": pa.array([1, 2], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")
    lance.write_dataset(table, uri, mode="append")
    return uri


@pytest.fixture
def settings(tmp_path: Path) -> MaintenanceSettings:
    control = tmp_path / "control"
    control.mkdir()
    return MaintenanceSettings(MAINTENANCE_CONTROL_ROOT=str(control), MAINTENANCE_POLICY_ROOT=str(control))


def _governed(uri: str, table: str, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> TableTask:
    """The task of the table ``uri`` holds, its claim taken as a catalog door takes one and its row named in the
    ``__manifest`` the pass reads."""
    uuid = str(uuid4())
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options={})
    location_claims.take(claims, normalise(uri), table, table.split("$"), is_gone=lambda _claim: False, uuid=uuid)
    named = TableIds(by_location={normalise(uri): table}, namespace_uuids={"db": str(uuid4())})
    monkeypatch.setattr(table_pass, "table_ids_of", lambda *_a, **_k: named)
    return TableTask(uuid=uuid, location=uri)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every dataset a pass reached its reclamation decision on: the probe before any vend counts what it could reclaim."""
    seen: list[str] = []
    real = table_pass.count_reclaimable

    def _spy(ds: lance.LanceDataset, policy: ExpiryPolicy) -> int:
        seen.append(ds.uri)
        return real(ds, policy)

    monkeypatch.setattr(table_pass, "count_reclaimable", _spy)
    return seen


@pytest.mark.parametrize("past_its_deadline", [pytest.param(False, id="inside-its-window"), pytest.param(True, id="past-its-deadline-and-unpurged")])
def test_a_trashed_tables_task_touches_nothing(
    tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch, reached: list[str], past_its_deadline: bool
) -> None:
    """THE ACCEPTANCE CRITERION: the dropped table, and its branch, are left alone."""
    dropped = _dataset(tmp_path / "dropped.lance")
    lance.dataset(dropped).create_branch("backfill")
    trashed = _governed(dropped, "db$dropped", settings, monkeypatch)
    dropped_at = datetime.now(UTC) - (timedelta(days=30) if past_its_deadline else timedelta(0))
    record = trash.make_record("db$dropped", kind="table", uuid=trashed.uuid, location=dropped, dropped_by="user:karin", grace_days=7, now=dropped_at)
    trash.put(str(settings.resolved_control_root), StorageOptions(), record)
    versions = [version["version"] for version in lance.dataset(dropped).versions()]

    frozen = run_table_pass(trashed, settings)

    assert (frozen.ended, frozen.next_due, reached) == ("trashed", None, []), "a pass reached a table the grace window promises is frozen"
    assert [version["version"] for version in lance.dataset(dropped).versions()] == versions


def test_an_unreadable_trash_raises_so_the_task_is_retried(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail toward NOT reclaiming, as the policy registry does: whether the table is in the trash decides whether it may
    be touched, so a read that cannot answer decides nothing, and the worker delivers the task again."""
    task = _governed(_dataset(tmp_path / "live.lance"), "db$live", settings, monkeypatch)

    def _unreachable(*_a: object, **_k: object) -> bool:
        raise OSError("control root unreachable")

    monkeypatch.setattr(table_pass.trash, "keeps", _unreachable)

    with pytest.raises(OSError, match="control root unreachable"):
        run_table_pass(task, settings)


def test_a_task_naming_a_table_at_anothers_location_leaves_that_tables_due_record(
    tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task's location is its announcer's word: one that names a table at a place another table holds says nothing of
    whether that table is gone, so its due record stands for its own pass to refresh."""
    standing = _governed(_dataset(tmp_path / "standing.lance"), "db$standing", settings, monkeypatch)
    elsewhere = _governed(_dataset(tmp_path / "elsewhere.lance"), "db$elsewhere", settings, monkeypatch)
    control = str(settings.resolved_control_root)
    due = maintenance_due.DueRecord(table="db$standing", uuid=standing.uuid, location=standing.location, version=2, evaluated_at=datetime.now(UTC))
    maintenance_due.write_due(control, StorageOptions(), due)

    misaddressed = run_table_pass(TableTask(uuid=standing.uuid, location=elsewhere.location), settings)

    assert (misaddressed.ended, maintenance_due.read_due(control, StorageOptions(), standing.uuid)) == ("gone", due)
