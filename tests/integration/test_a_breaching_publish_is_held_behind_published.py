"""A publish that breaches the project's quality gate is held: the version stays on main, `published` stays where
it was, and only a `can_promote` holder answers the hold (ADR 0156 C1, [[LH-466]]).

Readers of a governed tier read the version tagged `published`. The project declares its gate on the gates door
with review on, so a version whose row count moves past the band is not published: the door records a hold,
listed on `GET /management/v1/promotions`, and a validator approves it (`published` moves to the held version)
or rejects it (main is restored to the published version). A sweep expires a hold nobody answered.

On the real catalog app over real pylance with OIDC, FGA and warehouses on, the OpenFGA store an in-memory
double that allows exactly the grants each test names, so who may decide is part of every claim.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_clock, get_namespace
from catalog.api.promotion_expiry import mount_promotion_expiry
from catalog.core.config import Settings, get_settings
from catalog.core.lineage_emit import make_emitter
from catalog.services import object_uuids, table_claims, warehouses
from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy
from service_kit.governed import fga
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
NAMESPACE = "acme-silver"
TABLE = f"{NAMESPACE}$orders"
#: The run a silver write names (ADR 0156 C2): an input the writer may read, at a version.
RUN = {"x-rask-run-id": "run-7", "x-rask-inputs": json.dumps([{"table": ["acme-bronze", "orders"], "version": 1}])}
#: What the validator holds on the table, beside the writer rungs alice holds.
WRITER = ("can_write_data", "can_update_tag", "can_get_metadata", "can_read_data")


def _ipc(ids: range) -> bytes:
    table = pa.table({"id": pa.array(list(ids), pa.int64())})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


class Estate:
    """Project ``acme``'s silver table ``acme-silver$orders``, written and published by alice."""

    def __init__(self, client: TestClient, grants: Grants, root: str) -> None:
        self.client = client
        self.grants = grants
        self.root = root

    def declare_gate(self, *, review: bool) -> None:
        act_as(self.client, "alice")
        body = {"key_column": "id", "review_band": 0.25, "review_enabled": review}
        assert self.client.post(f"/management/v1/project/{PROJECT}/gate/set", json=body, headers=AUTH).status_code == 200

    def insert(self, ids: range) -> None:
        act_as(self.client, "alice")
        assert self.client.post(f"/v1/table/{TABLE}/insert", content=_ipc(ids), headers={**ARROW, **RUN}).status_code == 200

    def publish(self, version: int) -> dict[str, Any]:
        act_as(self.client, "alice")
        response = self.client.post(f"/management/v1/table/{TABLE}/publish", json={"version": version, "key_column": "id"}, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()

    def obj(self, kind: str, name: str) -> str:
        """The FGA object ``kind`` ``name`` is held by, the uuid the catalog minted for it ([[LH-524]]), read from the
        namespace backend and the claim store rather than through a door the grants under test would gate."""
        ns = self.client.app.dependency_overrides[get_namespace]()
        segments = name.split("$")
        if kind == "namespace":
            return fga.namespace_object(object_uuids.require_namespace_uuid(ns, segments))
        return fga.table_object(table_claims.resolve(ns, ClaimStore(control_root=self.root), segments).require_uuid())

    def location(self) -> str:
        act_as(self.client, "alice")
        described = self.client.post(f"/v1/table/{TABLE}/describe", json={}, headers=AUTH)
        assert described.status_code == 200, described.text
        return str(described.json()["location"])

    def rows(self, version: int | str | None = None) -> int:
        """The rows a reader of ``version`` (a number, a tag, or latest on main) sees."""
        return lance.dataset(self.location(), version=version).count_rows()

    def decide(self, sub: str, decision: str, version: int) -> httpx.Response:
        act_as(self.client, sub)
        return self.client.post(f"/management/v1/table/{TABLE}/promotion/{decision}", json={"version": version}, headers=AUTH)

    def holds(self, sub: str = "alice") -> list[tuple[str, int, int | None, list[str]]]:
        act_as(self.client, sub)
        listed = self.client.get("/management/v1/promotions", headers=AUTH)
        assert listed.status_code == 200, listed.text
        return [(h["table"], h["version"], h["published_version"], h["reasons"]) for h in listed.json()["holds"]]


@pytest.fixture
def estate(real_ns_client: TestClient, tmp_path: Path) -> Iterator[Estate]:
    """Version 1 of ``TABLE`` (4 rows) published under a gate with review off, then review turned on (band 0.25)."""
    root = str(tmp_path)
    settings = Settings.model_validate(
        {
            "root": root,
            "warehouses_enabled": True,
            "oidc_enabled": True,
            "oidc_connectors": {"local": "dex"},
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    client = real_ns_client
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.state.oidc = MagicMock()
    grants = Grants()
    client.app.state.fga = grants
    warehouses.put_warehouse(
        root, StorageOptions(), {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"}
    )
    grants.grant("alice", ("can_create_namespace", f"warehouse:{WAREHOUSE}"), ("can_administer", f"project:{PROJECT}"))
    act_as(client, "alice")
    declared = client.post(
        f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": NAMESPACE, "properties": {"rask.tier": "silver"}}, headers=AUTH
    )
    assert declared.status_code == 200, declared.text
    estate = Estate(client, grants, root)
    # The run's input, which the create names and alice reads.
    bronze = client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "acme-bronze"}, headers=AUTH)
    assert bronze.status_code == 200, bronze.text
    grants.grant("alice", ("can_create_table", estate.obj("namespace", NAMESPACE)), ("can_create_table", estate.obj("namespace", "acme-bronze")))
    assert client.post("/v1/table/acme-bronze$orders/create?mode=create", content=_ipc(range(4)), headers=ARROW).status_code == 200
    grants.grant("alice", ("can_read_data", estate.obj("table", "acme-bronze$orders")))
    estate.declare_gate(review=False)
    created = client.post(f"/v1/table/{TABLE}/create?mode=create", content=_ipc(range(4)), headers={**ARROW, **RUN})
    assert created.status_code == 200, created.text
    grants.grant("alice", *[(rung, estate.obj("table", TABLE)) for rung in WRITER])
    assert estate.publish(1)["published"] is True
    estate.declare_gate(review=True)
    yield estate


def test_a_breaching_publish_lands_on_main_and_readers_of_published_still_read_the_previous_version(estate: Estate) -> None:
    """Version 2 triples the rows, past the 0.25 band: the publish answers held, `@published` still reads version 1,
    main reads version 2, and the hold is listed for the validators to answer."""
    estate.insert(range(4, 12))

    answer = estate.publish(2)

    assert (answer["published"], answer["held"]) == (False, True), answer
    assert (estate.rows("published"), estate.rows()) == (4, 12)
    assert estate.holds() == [(TABLE, 2, 1, ["row_count_delta"])]


@pytest.fixture
def lineage(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lineage event the catalog posts, as the lineage service receives it, its tenant resolved from the
    warehouse binding as the app's own emitter resolves it."""
    posted: list[dict[str, Any]] = []

    def _receive(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    async def _project(top_ns: str) -> str | None:
        return warehouses.project_for_namespace(estate.root, StorageOptions(), top_ns)

    emitter = make_emitter(
        enabled=True,
        transport="http",
        url="http://lineage/api/v1/lineage",
        client=httpx.AsyncClient(transport=httpx.MockTransport(_receive)),
        dapr=None,
        pubsub="",
        topic="",
        job_namespace="lance-catalog",
        project_resolver=_project,
    )
    monkeypatch.setattr(estate.client.app.state, "lineage_emitter", emitter, raising=False)
    return posted


class _Inboxes:
    """Each subject's inbox as the notifications plane fills it."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        inboxes = self

        class _Inbox:
            async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
                inboxes.rows.setdefault(subject, []).append(payload)
                return {"delivered": True}

        return cast("TypedActorProxy", _Inbox())


def _told(event: dict[str, Any], *, watchers: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Who the notifications plane tells about ``event`` when the project's watchers are ``watchers``: subject ->
    ``(reason, object)`` rows."""
    inboxes = _Inboxes()

    async def _watching(project: str, _at: datetime) -> list[str]:
        return watchers if project == PROJECT else []

    status = asyncio.run(
        ingest_run_event(event, lane=Lane.BUS, visibility=Visibility(decider=None, enabled=False), watchers=_watching, open_inbox=inboxes.open)
    )
    assert status == {"status": "SUCCESS"}, status
    return {subject: [(row["reason"], row["object_id"]) for row in rows] for subject, rows in inboxes.rows.items()}


def test_approval_moves_published_and_tells_the_watchers_and_rejection_restores_main(estate: Estate, lineage: list[dict[str, Any]]) -> None:
    """bob, a validator, approves held version 2: `@published` reads it, and the run the catalog announces reaches
    the project's watcher. Version 3 is held next and bob rejects it: main is restored to the version `published`
    names, as version 4, `published` stays, and the restore is made once however often the rejection is repeated."""
    estate.grants.grant("bob", ("can_promote", estate.obj("table", TABLE)), ("can_get_metadata", estate.obj("table", TABLE)))
    estate.insert(range(4, 12))
    assert estate.publish(2)["held"] is True

    approved = estate.decide("bob", "approve", 2)

    assert approved.status_code == 200, approved.text
    assert (approved.json()["published_version"], approved.json()["hold"]["state"], approved.json()["hold"]["decided_by"]) == (2, "approved", "dex~bob")
    assert (estate.rows("published"), estate.holds()) == (12, [])
    announced = [event for event in lineage if event["run"]["facets"]["rask_lance"]["operation"] == "publish_table"]
    assert [event["outputs"][0]["facets"]["version"]["datasetVersion"] for event in announced] == ["2"]
    told = estate.obj("table", TABLE)
    assert _told(announced[-1], watchers=["dex~wendy"]) == {"dex~bob": [("author", told)], "dex~wendy": [("watch", told)]}

    estate.insert(range(12, 40))
    assert estate.publish(3)["held"] is True
    rejected = estate.decide("bob", "reject", 3)

    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["published_version"], rejected.json()["restored_version"], rejected.json()["hold"]["state"]) == (2, 4, "rejected")
    assert (estate.rows("published"), estate.rows(), estate.holds()) == (12, 12, [])
    # The rejection restored main once: rejecting again answers as decided and leaves the rows written since.
    estate.insert(range(40, 42))
    again = estate.decide("bob", "reject", 3)
    assert (again.status_code, again.json()["restored_version"], estate.rows()) == (200, 4, 14)


def test_without_can_promote_a_caller_can_neither_decide_a_hold_nor_move_published(estate: Estate) -> None:
    """carol writes, publishes and may move or drop the table's tags, but holds no `can_promote`: approving or
    rejecting the held version is PermissionDenied (15), moving or deleting `published` on the spec's tag doors is
    InvalidInput (13), and the hold, `published` and main stay as they were."""
    estate.grants.grant("carol", *[(rung, estate.obj("table", TABLE)) for rung in (*WRITER, "can_drop")])
    estate.insert(range(4, 12))
    assert estate.publish(2)["held"] is True

    decided = [estate.decide("carol", decision, 2) for decision in ("approve", "reject")]
    act_as(estate.client, "carol")
    moved = estate.client.post(f"/v1/table/{TABLE}/tags/update", json={"tag": "published", "version": 2}, headers=AUTH)
    dropped = estate.client.post(f"/v1/table/{TABLE}/tags/delete", json={"tag": "published"}, headers=AUTH)

    assert [(r.status_code, r.headers["content-type"], r.json().get("code")) for r in decided] == [(403, "application/problem+json", 15)] * 2
    assert [(r.status_code, r.headers["content-type"], r.json().get("code")) for r in (moved, dropped)] == [(400, "application/problem+json", 13)] * 2
    assert (estate.rows("published"), estate.rows(), estate.holds()) == (4, 12, [(TABLE, 2, 1, ["row_count_delta"])])


#: The expiry cron's Component name, which the chart also hands the catalog as the route it serves.
BINDING = "catalog-promotion-expiry-cron"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def sweep(estate: Estate) -> Iterator[Callable[[], httpx.Response]]:
    """The expiry cron mounted on the catalog as the chart names it, ticked on demand."""
    app = estate.client.app
    routes = list(app.router.routes)
    mount_promotion_expiry(app, BINDING)
    yield lambda: estate.client.post(f"/{BINDING}")
    app.router.routes[:] = routes


def test_a_hold_nobody_answers_is_expired_by_the_sweep(estate: Estate, sweep: Callable[[], httpx.Response]) -> None:
    """Held version 2 waits the gate's 72-hour review: a tick inside the window leaves it, the first tick past it
    expires the hold and unpins the version, and `published` and main stay as they were."""
    clock = _Clock()
    estate.client.app.dependency_overrides[get_clock] = lambda: clock
    estate.insert(range(4, 12))
    assert estate.publish(2)["held"] is True
    assert lance.dataset(estate.location()).tags.list()["rask.held"]["version"] == 2
    clock.now += timedelta(hours=71)
    assert sweep().json()["expired"] == 0

    clock.now += timedelta(hours=2)
    ticked = sweep()

    assert (ticked.status_code, ticked.json()["expired"]) == (200, 1)
    assert estate.holds() == []
    assert "rask.held" not in lance.dataset(estate.location()).tags.list()
    assert (estate.rows("published"), estate.rows()) == (4, 12)
