"""Every door the router guard leaves to its handler is decided there ([[LH-237]]).

`catalog.api.fga_deps.authorize` decides every route naming an `{id}` under a guarded resource, and the two batch
doors, before any handler runs (`test_an_undeclared_door_is_refused.py` walks those; `router_walk` draws the line).
Every other route under the router it authenticates and nothing more: the projects, warehouses, models, the estate
admin's authorization store, the decision door and every estate-wide listing. Their handlers decide them. A door
acting on one object asks OpenFGA about its caller and refuses: 403, or the not-found a destructive door answers so
that no caller learns which ids exist. A listing asks about each item and answers only those its caller may see, and a
listing behind a gate does so for every caller the gate admits. Nothing at the router tells whether a handler does
either, and a handler that forgets passes every other test, so one corpus holds a request for each such route, and the
drive fails on a route that falls through with no entry as it does on an entry whose route is gone.

Against the catalog as deployed with warehouses on, a loopback Dex signing callers in and a real OpenFGA deciding on a
store of the test's own ([[LH-235]]): a signed-in stranger holding no grant is refused at every door deciding one object,
by a decision the trail records for that request, and is shown none of the estate's objects at every listing.
`GET /management/v1/me` answers its caller about themself, and is the one route no grant decides.

Each door is correct on its own, too. `authorize` refuses a request with no client to ask (503) and one with no caller
(401) before any handler runs, and a deployed catalog does not boot without its client, so no handler meets either; but
a handler that read an authorization layer that is on as one that is off would let through whatever reaches it another
way. With that rung removed, every door refuses as the router would: 503 code 17 and 401 code 16, the spec's own
(`lance_docs/ns_catalog/namespace/operations/errors.md`).
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import router_walk
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from catalog.api import fga_deps
from catalog.api.dependencies import get_fga_client
from catalog.api.security import authenticate
from catalog.core.config import get_settings
from catalog.services import promotions, warehouses
from service_kit.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome
from service_kit.lakehouse import maintenance_due
from service_kit.lakehouse.maintenance_due import CompactionDue, DueRecord
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The estate the doors name: acme's warehouse, the two namespaces bound to it, a table in one, and a registry model.
PROJECT, WAREHOUSE, NAMESPACE, SIDE, TABLE, MODEL = "acme", "wh-acme", "lake", "side", "lake$t", "m"
#: A signed-in person holding no grant, and one who reads `SIDE` and nothing else.
STRANGER, READER = "mallory", "bob"

#: A refusal's status and the spec code a stock client reads (`lance_docs/ns_catalog/spec.yaml`): PermissionDenied, and
#: the TableNotFound a destructive door answers in its place.
_FORBIDDEN, _NOT_FOUND = (403, 15), (404, 4)
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))


class _Answered(BaseModel):
    """What a door answered one request, and whether the trail records an authorization decision refusing its caller."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    response: httpx.Response
    refused_by_a_decision: bool


class _Door(BaseModel):
    """A route the router leaves to its handler, a request naming the estate's objects, and the caller who sends it."""

    method: str
    route: str
    params: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    caller: str = STRANGER

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.route

    @property
    def request_id(self) -> str:
        return f"{self.caller} {self.method} {self.route}"

    def send(self, client: TestClient, headers: dict[str, str]) -> httpx.Response:
        """Send this door's request with ``headers``, under its request id, which the trail records."""
        path = router_walk.PARAM.sub(lambda match: self.params[match.group(1)], self.route)
        return client.request(self.method, path, headers={**headers, "X-Request-ID": self.request_id}, json=self.body)

    @abstractmethod
    def kept_out(self, answered: _Answered) -> bool:
        """Whether ``answered`` gave the caller nothing it holds no grant for."""


class _Refusing(_Door):
    """A door deciding one object: the caller is refused with this status and spec code, by a decision the trail records."""

    refused: tuple[int, int] = _FORBIDDEN

    def kept_out(self, answered: _Answered) -> bool:
        return _refusal(answered.response) == self.refused and answered.refused_by_a_decision


class _Listing(_Door):
    """A listing judging each item it would answer: the caller is answered this, naming no object it holds nothing on."""

    shown: Any

    def kept_out(self, answered: _Answered) -> bool:
        return answered.response.status_code == 200 and answered.response.json() == self.shown


def _refusal(answer: httpx.Response) -> tuple[int, int | None]:
    """The status of ``answer`` and the spec code of its problem body; an answer that is no problem body carries no code."""
    problem = answer.headers.get("content-type") == "application/problem+json"
    return answer.status_code, answer.json().get("code") if problem else None


_TUPLE = {"user": f"user:dex~{STRANGER}", "relation": "admin", "object": f"project:{PROJECT}"}

_DOORS: list[_Door] = [
    # The estate admin's authorization store, behind one gate on its router.
    _Refusing(method="GET", route="/management/v1/access/model"),
    _Refusing(method="GET", route="/management/v1/access/tuples"),
    _Refusing(method="POST", route="/management/v1/access/tuples", body=_TUPLE),
    _Refusing(method="DELETE", route="/management/v1/access/tuples", body=_TUPLE),
    _Refusing(method="POST", route="/management/v1/access/check", body=_TUPLE),
    _Refusing(method="POST", route="/management/v1/access/simulate", body=_TUPLE),
    _Refusing(method="POST", route="/management/v1/access/expand", body={"object": f"project:{PROJECT}", "relation": "admin"}),
    _Refusing(method="POST", route="/management/v1/access/list-users", body={"object": f"project:{PROJECT}", "relation": "admin"}),
    _Refusing(method="POST", route="/management/v1/access/list-objects", body={"user": "user:dex~alice", "relation": "admin", "type": "project"}),
    _Refusing(method="GET", route="/management/v1/access/principals/{principal:path}", params={"principal": "dex~alice"}),
    _Refusing(method="DELETE", route="/management/v1/access/principals/{principal:path}", params={"principal": "dex~alice"}),
    # The decision door, each route behind the role of the service that asks it.
    _Refusing(method="POST", route="/management/v1/decisions/check", body={"user": "user:dex~alice", "relation": "can_read_data", "tables": [TABLE]}),
    _Refusing(method="POST", route="/management/v1/decisions/list-users", body={"relation": "member", "object": f"project:{PROJECT}"}),
    _Refusing(method="POST", route="/management/v1/decisions/governed", body={"objects": ["table:00000000-0000-4000-8000-000000000000"]}),
    # The estate admin's feed and tenant registry.
    _Refusing(method="GET", route="/management/v1/events"),
    _Refusing(method="POST", route="/management/v1/projects", body={"id": "rival"}),
    _Refusing(method="GET", route="/management/v1/projects/{project_id}", params={"project_id": PROJECT}),
    # A tenant's own doors.
    _Refusing(method="DELETE", route="/management/v1/projects/{project_id}", params={"project_id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/projects/{project_id}/members", params={"project_id": PROJECT}),
    _Refusing(
        method="PUT",
        route="/management/v1/projects/{project_id}/members",
        params={"project_id": PROJECT},
        body={"user": f"dex~{STRANGER}", "relation": "admin"},
    ),
    _Refusing(
        method="DELETE",
        route="/management/v1/projects/{project_id}/members",
        params={"project_id": PROJECT},
        body={"user": "dex~alice", "relation": "admin"},
    ),
    _Refusing(method="POST", route="/management/v1/projects/{id}/access/my-permissions", params={"id": PROJECT}),
    _Refusing(method="GET", route="/management/v1/projects/{id}/policies", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/set", params={"id": PROJECT}, body={"retention_days": 7}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/describe", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/delete", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/gate/set", params={"id": PROJECT}, body={}),
    _Refusing(method="POST", route="/management/v1/project/{id}/gate/describe", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/gate/delete", params={"id": PROJECT}),
    # A warehouse's doors.
    _Refusing(method="POST", route="/management/v1/warehouses", body={"id": "wh-rival", "project": PROJECT}),
    _Refusing(method="GET", route="/management/v1/warehouses/{warehouse_id}", params={"warehouse_id": WAREHOUSE}),
    _Refusing(method="DELETE", route="/management/v1/warehouses/{warehouse_id}", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/activate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/deactivate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/validate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/warehouses/{warehouse_id}/namespaces", params={"warehouse_id": WAREHOUSE}),
    # The same listing behind its gate: bob reads `side` alone, which opens the warehouse to him through its `child` edge,
    # and is shown that namespace and not its sibling.
    _Listing(
        method="GET",
        route="/management/v1/warehouses/{warehouse_id}/namespaces",
        params={"warehouse_id": WAREHOUSE},
        caller=READER,
        shown={"namespaces": [SIDE]},
    ),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/namespaces", params={"warehouse_id": WAREHOUSE}, body={"namespace": "rival"}),
    _Refusing(
        method="DELETE",
        route="/management/v1/warehouses/{warehouse_id}/namespaces/{top_ns}",
        params={"warehouse_id": WAREHOUSE, "top_ns": NAMESPACE},
        refused=_NOT_FOUND,
    ),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/access/my-permissions", params={"id": WAREHOUSE}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/managed-access/describe", params={"id": WAREHOUSE}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/managed-access/set", params={"id": WAREHOUSE}, body={"enabled": True}),
    # The model registry: a registry model is no lance-namespace table, so its doors decide it themselves.
    _Refusing(method="GET", route="/management/v1/model/{model}", params={"model": MODEL}),
    _Refusing(method="POST", route="/management/v1/model/{model}/promote", params={"model": MODEL}, body={"version": 1}),
    # The listings.
    _Listing(method="GET", route="/management/v1/projects", shown=[]),
    _Listing(method="GET", route="/management/v1/warehouses", shown=[]),
    _Listing(method="GET", route="/management/v1/warehouses/-/bindings", shown={"bindings": {}}),
    _Listing(method="GET", route="/v1/table", shown={"tables": []}),
    _Listing(method="GET", route="/management/v1/model", shown={"models": [], "page_token": None}),
    _Listing(method="GET", route="/management/v1/erasures", shown={"obligations": [], "page_token": None}),
    _Listing(method="GET", route="/management/v1/maintenance/due", shown={"tables": [], "page_token": None}),
    _Listing(method="GET", route="/management/v1/promotions", shown={"holds": [], "page_token": None}),
]

#: The route that answers its caller about themself, which no grant decides.
_ABOUT_THE_CALLER = {("GET", "/management/v1/me")}


def _left_to_handlers() -> set[tuple[str, str]]:
    """Every method and route under the router that `authorize` authenticates and decides nothing about."""
    return {
        (method, path)
        for path, route in router_walk.mounted()
        if not router_walk.decided_by_the_router(path)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }


def _as_a_caller_sees_it(client: TestClient) -> TestClient:
    """A client on ``client``'s running app that answers a crash with the 500 a caller sees, rather than raising it, so a
    door that fails to refuse is named among the rest.

    It drives the app through ``client``'s portal, the event loop the lifespan opened the app's own clients on.
    """
    answering = TestClient(client.app, raise_server_exceptions=False)
    answering.portal = client.portal
    return answering


def _ok(answer: httpx.Response) -> httpx.Response:
    assert answer.status_code == 200, f"{answer.request.url}: {answer.status_code} {answer.text}"
    return answer


@pytest.fixture
def estate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    """The catalog as deployed with warehouses on, holding one of each object a door the router leaves open names.

    alice owns the estate, as the chart's bootstrap hook grants it, and makes ``acme``, the two namespaces bound to its
    warehouse, a table in one, an erasure obligation on that table and bob's grant on the other namespace through the
    doors. What no door on a ``dir`` estate makes is written where its writer keeps it: the warehouse's record and project
    edge (its door provisions a bucket and probes the store), maintenance's due record, a promotion hold (written by a
    publish a review gate holds) and a registry model (written by the pipeline that trains it).
    """
    monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
    catalog = request.getfixturevalue("governed_catalog")
    client, alice = catalog.client, catalog.bearer("alice")
    catalog.store.write((catalog.person("alice"), "owner", "estate:rask"))
    _ok(client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice))
    settings = get_settings()
    root, so, now = settings.registry_root, settings.storage_options(), datetime.now(UTC)
    record = {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": settings.root, "project": PROJECT, "status": "active", "created_at": now.isoformat()}
    warehouses.create_warehouse_record(root, so, record)
    catalog.store.write((f"project:{PROJECT}", "project", f"warehouse:{WAREHOUSE}"))
    for namespace in (NAMESPACE, SIDE):
        _ok(client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": namespace}, headers=alice))
    _ok(client.post(f"/management/v1/namespace/{SIDE}/access/grant", json={"user": f"dex~{READER}", "relation": "reader"}, headers=alice))
    _ok(client.post(f"/v1/table/{TABLE}/create", content=_ROWS, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE}))
    obliged = client.post(f"/management/v1/table/{TABLE}/erasure", json={"predicate": "id = 1"}, headers=alice)
    assert obliged.status_code == 202, obliged.text
    described = _ok(client.post(f"/v1/table/{TABLE}/describe", json={}, headers=alice)).json()
    uuid, location = described["properties"]["rask.uuid"], described["location"]
    held = promotions.Hold(
        table=TABLE,
        uuid=uuid,
        location=location,
        version=1,
        published_version=None,
        reasons=["row_count_delta"],
        requested_by=None,
        opened_at=now,
        expires_at=now + timedelta(days=1),
    )
    promotions.open_hold(root, so, held)
    compaction = CompactionDue(fragments=4, candidate_fragments=4, tasks=1)
    maintenance_due.write_due(root, so, DueRecord(table=TABLE, uuid=uuid, location=location, version=1, compaction=compaction, evaluated_at=now))
    published = pa.table({"artifact": ["weights"], "meta": [json.dumps({"config": {}, "features": [TABLE], "metrics": {"cer": 0.1}, "token": "t1"})]})
    lance.write_dataset(published, settings.model_uri(MODEL), data_storage_version="2.2", enable_stable_row_ids=True)
    return catalog


def test_every_door_the_router_leaves_to_its_handler_keeps_out_a_caller_it_grants_nothing(estate: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    left, client = _left_to_handlers(), _as_a_caller_sees_it(estate.client)

    answers = {door.request_id: door.send(client, estate.bearer(door.caller)) for door in _DOORS if door.key in left}

    refusals = {
        (r.__dict__["audit.request_id"], r.__dict__["audit.subject"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == AuditAction.AUTHZ and r.__dict__.get("audit.outcome") == AuditOutcome.DENY
    }
    let_through = {
        door.request_id: (answer.status_code, answer.text)
        for door in _DOORS
        if (answer := answers.get(door.request_id)) is not None
        and not door.kept_out(_Answered(response=answer, refused_by_a_decision=(door.request_id, f"dex~{door.caller}") in refusals))
    }
    entered = {door.key for door in _DOORS}
    assert (let_through, left - entered - _ABOUT_THE_CALLER, entered - left) == ({}, set(), set()), (
        "doors that let a caller through, routes the router leaves open with no entry, and entries naming no such route"
    )


async def _decides_nothing() -> None:
    """The router's own rung, removed."""


def _absent() -> None:
    """A dependency answering that what it provides is not there."""


@pytest.mark.parametrize(
    ("missing", "refused"),
    [
        pytest.param(get_fga_client, (503, 17), id="no-client-to-ask"),
        pytest.param(authenticate, (401, 16), id="no-caller-to-ask-about"),
    ],
)
def test_a_door_the_router_leaves_to_its_handler_refuses_on_its_own_when_authorization_cannot_decide(
    estate: Any, missing: Callable[..., object], refused: tuple[int, int]
) -> None:
    overrides = estate.client.app.dependency_overrides
    overrides[fga_deps.authorize] = _decides_nothing
    overrides[missing] = _absent
    client = _as_a_caller_sees_it(estate.client)

    answers = {door.request_id: _refusal(door.send(client, estate.bearer(door.caller))) for door in _DOORS}

    assert {request_id: answer for request_id, answer in answers.items() if answer != refused} == {}, "doors that did not refuse on their own"
