"""[[LH-228]] A drop is kept as its warehouse says, and its deadline releases its grants while its bytes await the purge.

A warehouse's ``delete_profile`` decides what a drop under it does (ADR 0146): ``hard`` destroys the table at once,
``soft`` keeps it recoverable for the profile's ``grace_days``, and a warehouse naming none, or naming ``estate``, takes
the estate's ``LANCE_TRASH_GRACE_DAYS``. The deadline is stamped on the trash record at the drop. Past it, the drop is
DropTable's, its data deleted asynchronously: the maintenance reconcile tick revokes the trashed object's grants,
announcing a ``grant_revoked`` per principal, and stamps the record released, on every tick and whether or not the purge
is on. The bytes stay for the purge, which the default chart ships off.

ONE ESTATE ON MOTO, TWO SERVICES (``catalog_and_maintenance``): the real catalog app drops through its door and files the
record, and the real maintenance app runs its reconcile tick over the same control root. One in-memory ``TupleStore``
stands behind both through the real ``service_kit.governed.fga`` functions, since the claim is which tuples remain. A
table still in its window and a live table whose undrop crashed before clearing its record are the controls: the tick
must keep both tables' grants.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest

from catalog.core.config import get_settings as catalog_settings
from service_kit.control_events import CatalogControlEvent
from service_kit.governed.fga import user_object
from service_kit.lakehouse import trash
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.catalog_and_maintenance import TwoServices, catalog_and_maintenance
from tests.integration.openfga_grants import principal_key


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _Captured:
    """The maintenance service's control emitter, keeping what it was handed."""

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

    def record(self, object_uuid: str, *, kind: str = "table") -> dict[str, Any] | None:
        settings = catalog_settings()
        return trash.get(settings.registry_root, settings.storage_options(), object_uuid, kind=kind)

    def put_record(self, record: dict[str, Any]) -> None:
        settings = catalog_settings()
        trash.put(settings.registry_root, settings.storage_options(), record)

    def age(self, object_uuid: str, *, kind: str = "table") -> None:
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
    FGA on and the purge off, as the default chart ships it; both ask one store."""
    captured = _Captured()
    env = {"LANCE_TRASH_GRACE_DAYS": "7", "MAINTENANCE_TRASH_PURGE_ENABLED": "false"}
    with catalog_and_maintenance(moto_url, monkeypatch, env=env, maintenance_fga=True, control=captured) as both:
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


def test_a_reconcile_tick_releases_the_grants_of_a_drop_past_its_deadline_and_keeps_its_bytes(estate: _Estate) -> None:
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

    answer = estate.maintenance.post("/maintenance-reconcile-cron")

    assert answer.status_code == 200, answer.text
    report = answer.json()["trash_release"]
    assert {f"{entry['kind']}:{entry['uuid']}" for entry in report["released"]} == released, report
    remaining = estate.store.naming(sorted(released | kept))
    assert not any(remaining.get(obj) for obj in released), f"an expired drop kept its grants: {remaining}"
    assert all(remaining.get(obj) == held[obj] for obj in kept), "a drop still in its window, or a live table, lost a grant"
    revoked = {(event.object_id, (event.extra or {}).get("subject")) for event in estate.control.events if event.action == "grant_revoked"}
    alice = user_object(principal_key("alice"))
    assert {(obj, alice) for obj in released} <= revoked, f"no grant_revoked told alice of each release: {revoked}"
    for obj in released:
        kind, object_uuid = obj.split(":")
        record = estate.record(object_uuid, kind=kind)
        assert record is not None and trash.is_released(record), f"the record of {obj} was not stamped released: {record}"
    assert estate.objects_at(location), "the tick deleted the bytes the purge owns"
    refused = estate.catalog.post("/management/v1/table/bronze$expired/undrop", json={})
    assert (refused.status_code, "grace period ended" in refused.text) == (409, True), f"an expired drop undropped: {refused.text}"
