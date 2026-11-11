"""The audit and the daily scan hand the reconciler what they read, and the scan's purge consumes the report it produced.

`test_reconcile_report.py` drives `reconcile()` directly, so it stays green for a reconciler handed nothing: with no grant
census four of its seven categories would report UNAVAILABLE in production while every unit test passed. These pin the
audit's WIRING instead of the logic (`maintenance.services.audit.run_audit`): the census it hands the reconciler, the
report its purge is gated on, and a missing client degrading rather than failing. One audit runs at a time because its
task is one subject on the work queue, which the queue's own lane proves on a real broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import respx
from pydantic import SecretStr

from lakehouse import location_claims, trash
from lakehouse.control_emit import ControlEmitter, NoopControlEmitter
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import audit
from maintenance.services.purge import TrashPurgeReport
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.reconcile import GrantCensusReader, ReconcileReport, Sources


#: These stubs return the real model: a hand-rolled mirror of the report must be edited whenever the thing it doubles
#: grows, and so eventually is not. Any fixed stamp: the audit never reads it, and a clock would make this
#: non-deterministic.
_CHECKED_AT = "2026-09-16T14:30:00+00:00"


def _settings(**over: Any) -> MaintenanceSettings:
    return MaintenanceSettings(s3_bucket="unit-bucket", s3_secret_access_key=SecretStr("unit"), **over)


class _Reconciler:
    """`reconcile` with its real signature: each call's arguments kept, and a report of its own answered to each."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.reports: list[ReconcileReport] = []

    async def __call__(
        self,
        settings: MaintenanceSettings,
        grants: GrantCensusReader | None = None,
        *,
        warehouses_enabled: bool,
        control_root: str | None = None,
        namespace_root: str | None = None,
        into_sources: list[Sources] | None = None,
        platform_buckets: Iterable[str] | None = None,
        bucket_client: object = None,
        walk: bool,
    ) -> ReconcileReport:
        self.calls.append(
            {
                "walk": walk,
                "grants": grants,
                "warehouses_enabled": warehouses_enabled,
                "control_root": control_root,
                "bucket_client": bucket_client,
            }
        )
        self.reports.append(ReconcileReport(checked_at=_CHECKED_AT))
        return self.reports[-1]


class _Purger:
    """`purge_expired_trash` with its real signature: the report each call was handed kept."""

    def __init__(self) -> None:
        self.reports: list[ReconcileReport] = []

    async def __call__(
        self,
        settings: MaintenanceSettings,
        *,
        report: ReconcileReport,
        control: ControlEmitter | None = None,
        control_root: str | None = None,
        data_roots: set[str] | None = None,
    ) -> TrashPurgeReport:
        self.reports.append(report)
        return TrashPurgeReport()


async def _the_hourly_audit(settings: MaintenanceSettings) -> dict[str, Any]:
    return await audit.run_audit(settings, queue=None)


async def _the_daily_scan(settings: MaintenanceSettings) -> dict[str, Any]:
    return await audit.run_scan(settings, control=NoopControlEmitter())


# --------------------------------------------------------------------------- #
# the clients — optional by design, so a missing one degrades rather than fails
# --------------------------------------------------------------------------- #


def test_the_audit_hands_the_reconciler_the_catalogs_grant_census(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring itself: the audit reads OpenFGA through the catalog's grant census, maintenance holding no OpenFGA
    credential ([[XC-084]]). Asserted because the failure mode is silent — an audit that quietly passes no census
    produces a report that is entirely 'unavailable' and still completes. The bucket-listing client is NOT handed
    over: the reconciler builds it per run with the key pair in force ([[XC-082]]), where one built at boot would list
    with a key the store has since replaced."""
    reconciler = _Reconciler()
    monkeypatch.setattr(audit, "reconcile", reconciler)
    settings = _settings(warehouses_enabled=True)

    out = asyncio.run(_the_daily_scan(settings))

    # The purge's key rides on every scan's payload; with the purge off it says it did not run.
    assert (out["total"], out["trash_purge"]["enabled"], out["trash_purge"]["ran"]) == (0, False, False)
    call = reconciler.calls[-1]
    assert call["grants"] is not None, "the scan handed the reconciler no grant census"
    # `warehouses_enabled` mirrors a CATALOG setting and is passed IN, never inferred: a divergent one reports every
    # namespace as unbound.
    assert (call["bucket_client"], call["warehouses_enabled"], call["control_root"]) == (None, True, settings.resolved_control_root)


@pytest.mark.parametrize(
    ("run", "scans_and_purges"),
    [pytest.param(_the_hourly_audit, False, id="the-hourly-audit"), pytest.param(_the_daily_scan, True, id="the-daily-scan")],
)
def test_only_the_daily_scan_walks_storage_and_purges(
    monkeypatch: pytest.MonkeyPatch, run: Callable[[MaintenanceSettings], Coroutine[Any, Any, dict[str, Any]]], scans_and_purges: bool
) -> None:
    """The walk lists every bucket's directories, so it runs daily (owner, 2026-11-09), and the purge with it: its gate
    takes only a report whose own run walked storage. The hourly audit asks the reconciler for no walk and runs no
    purge."""
    reconciler, purger = _Reconciler(), _Purger()
    monkeypatch.setattr(audit, "reconcile", reconciler)
    monkeypatch.setattr(audit, "purge_expired_trash", purger)

    out = asyncio.run(run(_settings()))

    assert ([call["walk"] for call in reconciler.calls], len(purger.reports), "trash_purge" in out) == (
        [scans_and_purges],
        int(scans_and_purges),
        scans_and_purges,
    )


def test_the_purge_consumes_the_report_its_own_audit_produced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is "the drift report ran clean", so the object it reads must be the one just produced.

    A purge fed a stored or previous report certifies a state that no longer exists — the estate could
    have drifted in between, and the whole permission model rests on the two being the same audit. Pinned
    by identity, which is the only way to state it that a later refactor cannot quietly weaken.
    """
    reconciler, purger = _Reconciler(), _Purger()
    monkeypatch.setattr(audit, "reconcile", reconciler)
    monkeypatch.setattr(audit, "purge_expired_trash", purger)

    out = asyncio.run(_the_daily_scan(_settings()))

    assert purger.reports[-1] is reconciler.reports[-1], "the purge read a report this scan did not produce"
    # The purge key rides on the scan's payload; its CONTENT is `test_trash_purge.py`'s subject, so this asserts only
    # that the real report reached the payload un-run.
    assert out["trash_purge"]["ran"] is False


def test_the_audit_asks_the_catalog_to_finish_every_clones_ending_it_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone's tag a door could not release is released by the catalog, asked by the audit for each ending the catalog
    lists (ADR 0198 §1): maintenance's store policy writes no source's tags. A refusal is reported by the ending's id and
    never stops the audit."""
    monkeypatch.setattr(audit, "reconcile", _Reconciler())
    catalog = "http://catalog.test"
    settings = _settings(catalog_url=catalog).model_copy(update={"control_root": str(tmp_path)})
    finished, refused = f"{'a' * 64}/{'c' * 32}", f"{'b' * 64}/{'c' * 32}"
    listed = [{"digest": d, "filing": f, "door": "deregister", "filed_at": _CHECKED_AT} for d, f in (e.split("/") for e in (finished, refused))]

    with respx.mock(base_url=catalog, assert_all_called=True) as doors:
        doors.get("/management/v1/clone-endings").mock(return_value=httpx.Response(200, json={"endings": listed}))
        done = doors.post(f"/management/v1/clone-endings/{finished}/finish").mock(return_value=httpx.Response(200, json={"tags_released": 1, "kept": False}))
        young = doors.post(f"/management/v1/clone-endings/{refused}/finish").mock(
            return_value=httpx.Response(409, json={"code": 19, "detail": "the ending was filed less than 0:15:00 ago"})
        )
        out = asyncio.run(_the_hourly_audit(settings))

    assert (done.call_count, young.call_count) == (1, 1)
    assert (out["clone_endings"]["finished"], list(out["clone_endings"]["refused"])) == ([finished], [refused])


def test_a_PLATFORM_bucket_is_never_reported_as_an_orphan() -> None:
    """A finding no operator can action keeps the drift report permanently non-clean.

    `orphan_buckets` reports buckets that exist in storage and that no warehouse record claims. The
    estate creates some for ITSELF — an `objectStore.buckets` entry the scoped-users hook provisions on the dev store —
    and no warehouse record will ever claim one, because they hold no governed tables.

    MEASURED live 2026-08-16: it sat in orphan_buckets on every run. Since `report_is_clean` blocks the
    #79 purge on ANY finding, the purge could not be reached by clearing real drift — only by deleting
    the observability store. `platform_buckets` defaulted to `sweep_buckets`, which is the set maintenance
    governs and says nothing about infrastructure it does not.

    The declared set comes from the chart's `lance.maintenancePlatformBuckets`: `lance.platformBuckets`,
    which the scoped-users hook creates, plus the multibase bases.
    """
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings(
        MAINTENANCE_S3_BUCKET="lance-catalog",
        MAINTENANCE_S3_PLATFORM_BUCKETS="extra-platform,lance-catalog",
    )
    assert "extra-platform" in s.platform_buckets, "the declared platform bucket must be exempt"
    assert "lance-catalog" in s.platform_buckets, "the swept set stays exempt — a maintained bucket is known by definition"
    assert "extra-platform" not in s.sweep_buckets, "a platform bucket must NOT become something maintenance governs"


class _HoldingQueue:
    """What the queue holds, as the audit reads it, and the tasks it is handed, with `TaskQueue`'s signatures."""

    def __init__(self, holding: dict[tuple[str, str], set[str]]) -> None:
        self._holding = holding
        self.queued: list[Task] = []
        self.scheduled: list[tuple[Task, datetime]] = []

    async def held(self, area: str, kind: str) -> set[str]:
        return set(self._holding.get((area, kind), set()))

    async def enqueue(self, task: Task) -> bool:
        self.queued.append(task)
        return True

    async def schedule_first(self, task: Task, at: datetime) -> bool:
        self.scheduled.append((task, at))
        return True


def test_the_audit_queues_what_neither_a_schedule_nor_the_queue_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backstop for every lost announcement: a table whose claim no schedule or task holds gets a pass, a trash record
    none holds gets its task, namespaces' included, and a trashed table gets no pass, since its trash task decides. A
    table holding no orphan check gets one, held within a day at the floor's interval, and a trashed table none. A record
    already released, or carrying no deadline, gets no task: its task would end holding nothing, and be queued again by
    every audit."""

    monkeypatch.setattr(audit, "reconcile", _Reconciler())
    settings = _settings().model_copy(update={"control_root": str(tmp_path)})
    claims = location_claims.ClaimStore(control_root=str(tmp_path), storage_options=StorageOptions())
    names = ("scheduled", "queued", "lost", "trashed", "released", "undated")
    tables = {name: (str(uuid4()), f"{tmp_path}/{name}") for name in names}
    for name, (table_uuid, location) in tables.items():
        location_claims.take(claims, location, f"db${name}", ["db", name], is_gone=lambda _claim: False, uuid=table_uuid)
    trashed_uuid, trashed_at = tables["trashed"]
    namespace_uuid = str(uuid4())
    released = trash.make_record("db$released", uuid=tables["released"][0], location=tables["released"][1], dropped_by=None, grace_days=7)
    released[trash.RELEASED_AT] = datetime.now(UTC).isoformat()
    undated = trash.make_record("db$undated", uuid=tables["undated"][0], location=tables["undated"][1], dropped_by=None, grace_days=7)
    del undated["expires_at"]
    for record in (
        trash.make_record("db$trashed", uuid=trashed_uuid, location=trashed_at, dropped_by=None, grace_days=7),
        trash.make_record("gone", uuid=namespace_uuid, location="", dropped_by=None, grace_days=7, kind="namespace"),
        released,
        undated,
    ):
        trash.put(str(tmp_path), StorageOptions(), record)
    queue = _HoldingQueue(
        {
            ("schedule", "table"): {tables["scheduled"][0]},
            ("task", "table"): {tables["queued"][0]},
            ("schedule", "orphans"): {tables["scheduled"][0]},
            ("task", "orphans"): {tables["lost"][0]},
        }
    )
    before = datetime.now(UTC)

    out = asyncio.run(audit.run_audit(settings, queue=cast(TaskQueue, queue)))

    lost_uuid, lost_at = tables["lost"]
    queued_uuid, queued_at = tables["queued"]
    assert out["unscheduled"] == {"tables": 1, "trash": 2, "checks": 1}
    ((check, at),) = queue.scheduled
    assert (check, before <= at < before + timedelta(days=1, minutes=1)) == (Task(kind="orphans", uuid=queued_uuid, location=queued_at), True)
    assert sorted(queue.queued, key=lambda task: (task.kind, task.uuid)) == sorted(
        [
            Task(kind="table", uuid=lost_uuid, location=lost_at),
            Task(kind="trash", uuid=trashed_uuid, location=trashed_at, trashed="table"),
            Task(kind="trash", uuid=namespace_uuid, trashed="namespace"),
        ],
        key=lambda task: (task.kind, task.uuid),
    )
