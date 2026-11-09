"""[[LH-228]] A drop is kept as its warehouse says, and its deadline releases its grants while its bytes await the purge.

A warehouse's ``delete_profile`` decides what a drop under it does (ADR 0146): ``hard`` destroys the table at once,
``soft`` keeps it recoverable for the profile's ``grace_days``, and a warehouse naming none, or naming ``estate``, takes
the estate's ``LANCE_TRASH_GRACE_DAYS``. The deadline is stamped on the trash record at the drop. Past it, the drop is
DropTable's, its data deleted asynchronously: on maintenance's audit the catalog revokes the trashed object's grants,
announcing a ``grant_revoked`` per principal under its own signature, and stamps the record released ([[XC-084]]), on
every audit and whether or not the purge is on. The bytes stay for the purge, which the default chart
ships off.

ONE ESTATE ON MOTO, TWO SERVICES (``catalog_and_maintenance``): the real catalog app drops through its door and files the
record, and the real maintenance app runs its audit over the same control root, asking the catalog's own
release door (``catalog_doors``). One in-memory ``TupleStore`` stands behind the catalog through the real
``lakehouse.governed.fga`` functions, since the claim is which tuples remain. A table still in its window and a live
table whose undrop crashed before clearing its record are the controls: the audit must keep both tables' grants.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pyarrow as pa
import pytest

from catalog.core.config import get_settings as catalog_settings
from lakehouse import trash
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed.fga import user_object
from maintenance.core.config import get_settings as maintenance_settings
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.trash_task import run_trash_task
from tests.integration.catalog_and_maintenance import TwoServices, audited, catalog_and_maintenance
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors
from tests.integration.openfga_grants import principal_key


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _Captured:
    """The catalog's control emitter, keeping what it was handed."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


class _Estate:
    """Project ``acme``, driven by its admin through the catalog, and the maintenance service beside it."""

    def __init__(self, both: TwoServices, control: _Captured) -> None:
        self.catalog = both.catalog
        self.maintenance = both.maintenance
        self.store = both.store
        self.s3 = both.s3
        self.control = control

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self.catalog.post(path, json=body or {})
        assert answer.status_code == 200, f"{path}: {answer.text}"
        return answer.json()

    def warehouse(self, warehouse: str, namespace: str, profile: dict[str, Any] | None = None) -> None:
        """``warehouse``, in its own bucket, under ``profile``, with ``namespace`` bound to it."""
        self.post("/management/v1/warehouses", {"id": warehouse, "project": PROJECT, **({"delete_profile": profile} if profile else {})})
        self.post(f"/management/v1/warehouses/{warehouse}/namespaces", {"namespace": namespace})

    def create_table(self, table: str) -> str:
        """``table``, created with two rows; the uuid the catalog minted for it."""
        answer = self.catalog.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, answer.text
        return str(self.post(f"/v1/table/{table}/describe")["properties"]["rask.uuid"])

    def location(self, table: str) -> str:
        return str(self.post(f"/v1/table/{table}/describe")["location"])

    def record(self, object_uuid: str, *, kind: trash.TrashKind = "table") -> dict[str, Any] | None:
        settings = catalog_settings()
        return trash.get(settings.registry_root, settings.storage_options(), object_uuid, kind=kind)

    def put_record(self, record: dict[str, Any]) -> None:
        settings = catalog_settings()
        trash.put(settings.registry_root, settings.storage_options(), record)

    def age(self, object_uuid: str, *, kind: trash.TrashKind = "table") -> None:
        """Move the deadline of the record ``object_uuid`` keeps a day into the past: a drop whose window has ended."""
        record = self.record(object_uuid, kind=kind)
        assert record is not None, f"no {kind} record for {object_uuid}"
        self.put_record({**record, "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()})

    def objects_at(self, location: str) -> list[str]:
        bucket, _, prefix = location.removeprefix("s3://").partition("/")
        listed = self.s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix.rstrip('/')}/")
        return [str(item["Key"]) for item in listed.get("Contents", [])]


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    """The catalog with the estate's seven-day grace period, as alice, and maintenance reading the same control root with
    the purge off, as the default chart ships it."""
    captured = _Captured()
    env = {"LANCE_TRASH_GRACE_DAYS": "7", "MAINTENANCE_TRASH_PURGE_ENABLED": "false", "MAINTENANCE_CATALOG_URL": CATALOG_URL}
    with catalog_and_maintenance(moto_url, monkeypatch, env=env, catalog_control=captured) as both:
        answer = both.catalog.post("/management/v1/projects", json={"id": PROJECT})
        assert answer.status_code == 200, answer.text
        yield _Estate(both, captured)


def test_a_drop_under_a_warehouse_is_kept_as_its_delete_profile_says(estate: _Estate) -> None:
    suffix = uuid.uuid4().hex[:6]
    estate.warehouse(f"hard-{suffix}", "hot", {"mode": "hard"})
    estate.warehouse(f"soft-{suffix}", "warm", {"mode": "soft", "grace_days": 2})
    destroyed = estate.create_table("hot$t")
    kept = estate.create_table("warm$t")
    destroyed_at = estate.location("hot$t")

    estate.post("/v1/table/hot$t/drop")
    trashed = estate.post("/v1/table/warm$t/drop")
    reposted = estate.post("/management/v1/warehouses", {"id": f"soft-{suffix}", "project": PROJECT})
    cleared = estate.post("/management/v1/warehouses", {"id": f"soft-{suffix}", "project": PROJECT, "delete_profile": {"mode": "estate"}})

    assert estate.record(destroyed) is None and estate.objects_at(destroyed_at) == [], "a hard warehouse's drop kept the table"
    record = estate.record(kept)
    assert record is not None and trashed.get("transaction_id") == f"warm$t${kept}", trashed
    grace = datetime.fromisoformat(str(record["expires_at"])) - datetime.fromisoformat(str(record["dropped_at"]))
    assert grace == timedelta(days=2), f"the soft warehouse's drop kept the estate's grace, not its own: {grace}"
    assert reposted["delete_profile"] == {"mode": "soft", "grace_days": 2}, "a re-POST naming no profile reset the warehouse's"
    assert "delete_profile" not in cleared, f"a re-POST naming the estate's profile kept the warehouse's own: {cleared}"


def test_an_audit_releases_the_grants_of_a_drop_past_its_deadline_and_keeps_its_bytes(estate: _Estate) -> None:
    estate.warehouse(f"wh-{uuid.uuid4().hex[:6]}", "bronze")
    expired, waiting, live = (estate.create_table(f"bronze${name}") for name in ("expired", "waiting", "live"))
    estate.post("/v1/namespace/bronze$gone/create")
    nested = estate.create_table("bronze$gone$t")
    namespace = str(estate.post("/v1/namespace/bronze$gone/describe")["properties"]["rask.uuid"])
    location = estate.location("bronze$expired")
    estate.post("/v1/table/bronze$expired/drop")
    estate.post("/v1/table/bronze$waiting/drop")
    estate.post("/v1/namespace/bronze$gone/drop", {"behavior": "Cascade"})
    estate.age(expired)
    estate.age(nested)
    estate.age(namespace, kind="namespace")
    past = datetime.now(UTC) - timedelta(days=1)
    estate.put_record(trash.make_record("bronze$live", uuid=live, location=estate.location("bronze$live"), dropped_by=None, grace_days=0, now=past))
    released = {f"table:{expired}", f"table:{nested}", f"namespace:{namespace}"}
    kept = {f"table:{waiting}", f"table:{live}"}
    held = estate.store.naming(sorted(released | kept))
    assert all(held.get(obj) for obj in released | kept), f"a drop kept no grant, so releasing them would pass vacuously: {held}"

    with catalog_doors(estate.catalog):
        report = audited(estate.maintenance)["trash_release"]
    assert {f"{entry['kind']}:{entry['uuid']}" for entry in report["released"]} == released, report
    remaining = estate.store.naming(sorted(released | kept))
    assert not any(remaining.get(obj) for obj in released), f"an expired drop kept its grants: {remaining}"
    assert all(remaining.get(obj) == held[obj] for obj in kept), "a drop still in its window, or a live table, lost a grant"
    revoked = {(event.object_id, (event.extra or {}).get("subject")) for event in estate.control.events if event.action == "grant_revoked"}
    alice = user_object(principal_key("alice"))
    assert {(obj, alice) for obj in released} <= revoked, f"no grant_revoked told alice of each release: {revoked}"
    for obj in released:
        prefix, object_uuid = obj.split(":")
        record = estate.record(object_uuid, kind="namespace" if prefix == "namespace" else "table")
        assert record is not None and trash.is_released(record), f"the record of {obj} was not stamped released: {record}"
    assert estate.objects_at(location), "the tick deleted the bytes the purge owns"
    refused = estate.catalog.post("/management/v1/table/bronze$expired/undrop", json={})
    assert (refused.status_code, "grace period ended" in refused.text) == (409, True), f"an expired drop undropped: {refused.text}"


class _RecordingQueue:
    """The publishes a trash task makes, with `TaskQueue`'s signatures, recorded."""

    def __init__(self) -> None:
        self.held: list[tuple[Task, datetime]] = []
        self.queued: list[Task] = []

    async def schedule_at(self, task: Task, at: datetime) -> None:
        self.held.append((task, at))

    async def enqueue(self, task: Task) -> bool:
        self.queued.append(task)
        return True


def test_a_drops_trash_task_holds_itself_until_its_release_then_releases_its_record_alone(estate: _Estate) -> None:
    """The task the commit lane queues for a drop names its uuid alone. Before the deadline it holds itself until the
    record's release, carrying the record's location for the undrop check its later run makes; past the release it asks
    the catalog to release this record and no other ([[XC-084]])."""
    estate.warehouse(f"wh-{uuid.uuid4().hex[:6]}", "bronze")
    dropped, other = (estate.create_table(f"bronze${name}") for name in ("dropped", "other"))
    location = estate.location("bronze$dropped")
    estate.post("/v1/table/bronze$dropped/drop")
    estate.post("/v1/table/bronze$other/drop")
    task = Task(kind="trash", uuid=dropped)
    queue = _RecordingQueue()

    asyncio.run(run_trash_task(task, settings=maintenance_settings(), queue=cast(TaskQueue, queue)))

    filed = estate.record(dropped)
    assert filed is not None and queue.held == [(task.model_copy(update={"location": location}), trash.release_at(filed))], queue.held
    estate.age(dropped)
    estate.age(other)

    with catalog_doors(estate.catalog):
        asyncio.run(run_trash_task(task, settings=maintenance_settings(), queue=cast(TaskQueue, queue)))

    released, waiting = estate.record(dropped), estate.record(other)
    assert released is not None and trash.is_released(released), f"the task did not release its record: {released}"
    assert waiting is not None and not trash.is_released(waiting), "the task released a record it does not name"
    assert (len(queue.held), queue.queued) == (1, []), "a released record held or queued anything more"
