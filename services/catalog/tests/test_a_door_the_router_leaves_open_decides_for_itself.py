"""Every door the router guard leaves to its handler is decided there ([[LH-237]]).

`catalog.api.fga_deps.authorize` decides every route naming an `{id}` under a guarded resource, and the two batch
doors, before any handler runs (`test_an_undeclared_door_is_refused.py` walks those; `router_walk` draws the line).
Every other route under the router it authenticates and nothing more: the projects, warehouses, models, the estate
admin's authorization store, the decision door and every estate-wide listing. Their handlers decide them. A door
acting on one object asks OpenFGA about its caller and refuses: 403, or the not-found a door answers where a 403 would
tell its caller which ids exist. A listing asks about each item and answers only those its caller may see, and a
listing behind a gate does so for every caller the gate admits. Nothing at the router tells whether a handler does
either, and a handler that forgets passes every other test, so one corpus holds a request for each such route, and the
drive fails on a route that falls through with no entry as it does on an entry whose route is gone.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a
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

import logging
import re
from abc import abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import lance_namespace
import pyarrow as pa
import pytest
import router_walk
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from catalog.api import fga_deps
from catalog.api.dependencies import get_fga_client
from catalog.api.security import authenticate
from catalog.core.config import get_settings
from catalog.services import warehouses
from lakehouse import maintenance_due
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome
from lakehouse.maintenance_due import CompactionDue, DueRecord
from tests.traces import trace_named, traceparent_named


#: The estate the doors name: acme's warehouse, two namespaces in it, and a table in one.
PROJECT, WAREHOUSE, NAMESPACE, SIDE, TABLE = "acme", "wh-acme", "wh-acme$lake", "wh-acme$side", "wh-acme$lake$t"
#: A role id the role doors are asked about: a stranger is refused on it by a decision, whatever it names.
ROLE = "00000000-0000-4000-8000-000000000001"
#: A signed-in person holding no grant, and one who reads `SIDE` and nothing else.
STRANGER, READER = "mallory", "bob"

#: A refusal's status and the spec code a stock client reads (`lance_docs/ns_catalog/spec.yaml`): PermissionDenied, and
#: the TableNotFound a door answers in its place where a 403 would tell its caller an id exists.
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
        """Send this door's request with ``headers``, in the trace its request id names, which the trail records."""
        path = router_walk.PARAM.sub(lambda match: self.params[match.group(1)], self.route)
        return client.request(self.method, path, headers={**headers, **traceparent_named(self.request_id)}, json=self.body)

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
    # The decision door, each route but `check` behind the role of the service that asks it.
    _Refusing(method="POST", route="/management/v1/decisions/list-users", body={"relation": "member", "object": f"project:{PROJECT}"}),
    _Refusing(method="POST", route="/management/v1/decisions/governed", body={"objects": ["table:00000000-0000-4000-8000-000000000000"]}),
    _Refusing(method="POST", route="/management/v1/decisions/locate", body={"locations": ["s3://lakehouse/00000000-0000-4000-8000-000000000000"]}),
    # The estate admin's feed and tenant registry.
    _Refusing(method="GET", route="/management/v1/events"),
    _Refusing(method="POST", route="/management/v1/projects", body={"id": "rival"}),
    # A tenant's own doors.
    _Refusing(method="GET", route="/management/v1/projects/{project_id}", params={"project_id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(method="DELETE", route="/management/v1/projects/{project_id}", params={"project_id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(
        method="POST",
        route="/management/v1/projects/{project_id}/protection",
        params={"project_id": PROJECT},
        body={"protected": True},
        refused=_NOT_FOUND,
    ),
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
    # A project's roles ([[LH-557]]): each refusal answers the missing project's or role's 404.
    _Refusing(method="POST", route="/management/v1/projects/{project_id}/roles", params={"project_id": PROJECT}, body={"name": "rival"}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/projects/{project_id}/roles", params={"project_id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/projects/{project_id}/roles/{role_id}", params={"project_id": PROJECT, "role_id": ROLE}, refused=_NOT_FOUND),
    _Refusing(
        method="DELETE", route="/management/v1/projects/{project_id}/roles/{role_id}", params={"project_id": PROJECT, "role_id": ROLE}, refused=_NOT_FOUND
    ),
    _Refusing(
        method="PUT",
        route="/management/v1/projects/{project_id}/roles/{role_id}/assignees",
        params={"project_id": PROJECT, "role_id": ROLE},
        body={"user": f"dex~{STRANGER}"},
        refused=_NOT_FOUND,
    ),
    _Refusing(
        method="DELETE",
        route="/management/v1/projects/{project_id}/roles/{role_id}/assignees",
        params={"project_id": PROJECT, "role_id": ROLE},
        body={"user": "dex~alice"},
        refused=_NOT_FOUND,
    ),
    _Refusing(method="POST", route="/management/v1/projects/{id}/access/my-permissions", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/projects/{id}/access/list", params={"id": PROJECT}, body={}),
    _Refusing(method="POST", route="/management/v1/projects/{id}/access/grant", params={"id": PROJECT}, body={"user": "dex~erin", "relation": "decider"}),
    _Refusing(method="POST", route="/management/v1/projects/{id}/access/revoke", params={"id": PROJECT}, body={"user": "dex~erin", "relation": "decider"}),
    _Refusing(method="GET", route="/management/v1/projects/{id}/policies", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/set", params={"id": PROJECT}, body={"retention_days": 7}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/describe", params={"id": PROJECT}),
    _Refusing(method="POST", route="/management/v1/project/{id}/policy/delete", params={"id": PROJECT}),
    _Refusing(method="GET", route="/management/v1/projects/{id}/labels", params={"id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/projects/{id}/labels/objects", params={"id": PROJECT}, refused=_NOT_FOUND),
    _Refusing(
        method="POST", route="/management/v1/projects/{id}/labels/define", params={"id": PROJECT}, body={"name": "pii", "scope": ["table"], "kind": "marker"}
    ),
    _Refusing(method="POST", route="/management/v1/projects/{id}/labels/{name}/delete", params={"id": PROJECT, "name": "pii"}),
    # A warehouse's doors.
    _Refusing(method="POST", route="/management/v1/warehouses", body={"id": "wh-rival", "project": PROJECT}),
    _Refusing(method="GET", route="/management/v1/warehouses/{warehouse_id}", params={"warehouse_id": WAREHOUSE}),
    _Refusing(method="DELETE", route="/management/v1/warehouses/{warehouse_id}", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/activate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/deactivate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(
        method="POST",
        route="/management/v1/warehouses/{warehouse_id}/protection",
        params={"warehouse_id": WAREHOUSE},
        body={"protected": False},
        refused=_NOT_FOUND,
    ),
    _Refusing(method="POST", route="/management/v1/warehouses/{warehouse_id}/validate", params={"warehouse_id": WAREHOUSE}, refused=_NOT_FOUND),
    _Refusing(method="GET", route="/management/v1/warehouses/{warehouse_id}/trash", params={"warehouse_id": WAREHOUSE}),
    # Maintenance's reclaim of a trash record, every door asked before any record is read, so the uuid names none.
    _Refusing(method="POST", route="/management/v1/trash/{kind}/{uuid}/credentials", params={"kind": "table", "uuid": ROLE}),
    _Refusing(method="POST", route="/management/v1/trash/{kind}/{uuid}/release", params={"kind": "table", "uuid": ROLE}),
    _Refusing(method="DELETE", route="/management/v1/trash/{kind}/{uuid}", params={"kind": "table", "uuid": ROLE}),
    _Refusing(method="POST", route="/management/v1/trash/{kind}/{uuid}/refusals", params={"kind": "table", "uuid": ROLE}, body={"reason": "a stranger's"}),
    # Maintenance's finish of a clone's ending, on the same rung, asked before any ending is read.
    _Refusing(method="GET", route="/management/v1/clone-endings"),
    _Refusing(method="POST", route="/management/v1/clone-endings/{digest}/{filing}/finish", params={"digest": "c" * 64, "filing": "d" * 32}),
    # Maintenance's reclaim of expired idempotency records, on the same rung ([[LH-576]]).
    _Refusing(method="POST", route="/management/v1/idempotency-keys/reclaim"),
    # Maintenance's finish of what a failed door left to announce, on the same rung ([[LH-576]]).
    _Refusing(method="POST", route="/management/v1/announcement-intents/finish"),
    # Maintenance's drift reconciler reads and repairs the estate's grants through these ([[XC-084]]).
    _Refusing(method="GET", route="/management/v1/grants/census"),
    _Refusing(method="POST", route="/management/v1/grants/ghosts/revoke", body={"objects": [f"project:{PROJECT}"]}),
    _Refusing(method="POST", route="/management/v1/grants/tenancy/rebuild", body={"warehouses": [WAREHOUSE]}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/access/my-permissions", params={"id": WAREHOUSE}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/access/list", params={"id": WAREHOUSE}, body={}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/access/grant", params={"id": WAREHOUSE}, body={"user": "dex~erin", "relation": "reader"}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/access/revoke", params={"id": WAREHOUSE}, body={"user": "dex~erin", "relation": "reader"}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/managed-access/describe", params={"id": WAREHOUSE}),
    _Refusing(method="POST", route="/management/v1/warehouse/{id}/managed-access/set", params={"id": WAREHOUSE}, body={"enabled": True}),
    # The listings, and the decision door's check, which answers only what lies inside its caller's scope ([[LH-563]]).
    _Listing(
        method="POST",
        route="/management/v1/decisions/check",
        body={"user": "dex~alice", "relation": "can_read_data", "tables": [TABLE]},
        shown={"allowed": {}, "tables": {TABLE: {"allowed": False, "table_uuid": None, "namespace_uuid": None}}},
    ),
    _Listing(method="GET", route="/management/v1/projects", shown=[]),
    _Listing(method="GET", route="/management/v1/warehouses", shown=[]),
    _Listing(method="GET", route="/v1/table", shown={"tables": []}),
    _Listing(method="GET", route="/management/v1/erasures", shown={"obligations": [], "page_token": None}),
    _Listing(method="GET", route="/management/v1/maintenance/due", shown={"tables": [], "page_token": None}),
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
def estate(request: pytest.FixtureRequest) -> Any:
    """The catalog as deployed, holding one of each object a door the router leaves open names.

    alice owns the estate, as the catalog's declaration grants it, and makes ``acme``, the two namespaces in its
    warehouse, a table in one, an erasure obligation on that table and bob's grant on the other namespace through the
    doors. What no door on a ``dir`` estate makes is written where its writer keeps it: the warehouse's record, its row in
    its root's catalog and its project edge (its door provisions a bucket and probes the store), and maintenance's due record.
    """
    catalog = request.getfixturevalue("governed_catalog")
    client, alice = catalog.client, catalog.bearer("alice")
    catalog.store.write((catalog.person("alice"), "admin", "estate:rask"))
    _ok(client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice))
    settings = get_settings()
    root, so, now = settings.registry_root, settings.storage_options(), datetime.now(UTC)
    record = {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": settings.root, "project": PROJECT, "status": "active", "created_at": now.isoformat()}
    warehouses.put_warehouse(root, so, record)
    warehouses.ensure_catalog_row(lance_namespace.connect("dir", {"root": settings.root}), WAREHOUSE)
    # Live, as its create door leaves it (ADR 0245): its tenancy edge, between the two creations.
    catalog.store.write((catalog.creation("project", PROJECT), "project", catalog.creation("warehouse", WAREHOUSE)))
    for namespace in (NAMESPACE, SIDE):
        _ok(client.post(f"/v1/namespace/{namespace}/create", json={}, headers=alice))
    _ok(client.post(f"/management/v1/namespace/{SIDE}/access/grant", json={"user": f"dex~{READER}", "relation": "reader"}, headers=alice))
    _ok(client.post(f"/v1/table/{TABLE}/create", content=_ROWS, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE}))
    obliged = client.post(f"/management/v1/table/{TABLE}/erasure", json={"predicate": "id = 1"}, headers=alice)
    assert obliged.status_code == 202, obliged.text
    described = _ok(client.post(f"/v1/table/{TABLE}/describe", json={}, headers=alice)).json()
    uuid, location = described["properties"]["rask.uuid"], described["location"]
    compaction = CompactionDue(fragments=4, candidate_fragments=4, tasks=1)
    maintenance_due.write_due(root, so, DueRecord(table=TABLE, uuid=uuid, location=location, version=1, compaction=compaction, evaluated_at=now))
    return catalog


def test_every_door_the_router_leaves_to_its_handler_keeps_out_a_caller_it_grants_nothing(estate: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    left, client = _left_to_handlers(), _as_a_caller_sees_it(estate.client)

    answers = {door.request_id: door.send(client, estate.bearer(door.caller)) for door in _DOORS if door.key in left}

    refusals = {
        (r.__dict__["audit.trace_id"], r.__dict__["audit.subject"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == AuditAction.AUTHZ and r.__dict__.get("audit.outcome") == AuditOutcome.DENY
    }
    let_through = {
        door.request_id: (answer.status_code, answer.text)
        for door in _DOORS
        if (answer := answers.get(door.request_id)) is not None
        and not door.kept_out(_Answered(response=answer, refused_by_a_decision=(trace_named(door.request_id), f"dex~{door.caller}") in refusals))
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


#: A door on a project's or a warehouse's id: every one of them is gated through the one gate (``fga_deps.on_tenant``).
_TENANCY_DOOR = re.compile(r"^/management/v1/(projects?|warehouses?)/\{")
#: An id no project or warehouse holds, in place of the estate's own.
_ABSENT = {PROJECT: "nope", WAREHOUSE: "wh-nope"}


def _for_an_absent_id(door: _Door) -> _Door:
    """``door`` asked about an id no project or warehouse holds, in its path or, for a warehouse's create, its body."""
    params = {name: _ABSENT.get(value, value) for name, value in door.params.items()}
    body = {key: _ABSENT.get(value, value) if key == "project" else value for key, value in door.body.items()} if door.body is not None else None
    return door.model_copy(update={"params": params, "body": body})


def test_a_tenancy_door_answers_a_caller_it_grants_nothing_alike_for_an_id_no_object_holds(estate: Any) -> None:
    """ADR 0245 decision 4, review T4: a door on a project's or a warehouse's id is asked its gate on the creation the id
    names, or on the object nobody holds where none stands, so a caller holding nothing is answered the same for an id that
    stands and one that does not, and learns nothing about which ids exist. Every such door the router serves is asked."""
    client = _as_a_caller_sees_it(estate.client)
    tenancy_doors = {(method, route) for method, route in _left_to_handlers() if _TENANCY_DOOR.match(route)} | {("POST", "/management/v1/warehouses")}
    asked = [door for door in _DOORS if door.key in tenancy_doors]

    def answered(door: _Door) -> tuple[int, int | None]:
        return _refusal(door.send(client, estate.bearer(door.caller)))

    answers = {door.request_id: (answered(door), answered(_for_an_absent_id(door))) for door in asked}

    assert {door.key for door in asked} == tenancy_doors, "a tenancy door the router serves with no entry in the corpus"
    assert {request_id: pair for request_id, pair in answers.items() if pair[0] != pair[1] or pair[0][0] < 400} == {}, (
        "doors answering a caller who holds nothing differently for an id that stands and one that does not (standing, absent)"
    )


#: Every tenancy door that writes a tuple naming a project's or a warehouse's creation, as its admin sends it, and what it
#: needs standing first: each resolves the id again once its Write has landed (``tenancy.require_still``, review T6).
_ROLE_PARAMS = {"project_id": PROJECT, "role_id": "{role}"}
_WRITING: dict[tuple[str, str], tuple[dict[str, str], dict[str, Any] | None, str | None]] = {
    ("PUT", "/management/v1/projects/{project_id}/members"): ({"project_id": PROJECT}, {"user": f"dex~{READER}", "relation": "member"}, None),
    ("DELETE", "/management/v1/projects/{project_id}/members"): ({"project_id": PROJECT}, {"user": f"dex~{READER}", "relation": "member"}, "member"),
    ("POST", "/management/v1/projects/{id}/access/grant"): ({"id": PROJECT}, {"user": "dex~erin", "relation": "decider"}, None),
    ("POST", "/management/v1/projects/{id}/access/revoke"): ({"id": PROJECT}, {"user": "dex~erin", "relation": "decider"}, "decider"),
    ("POST", "/management/v1/projects/{project_id}/roles"): ({"project_id": PROJECT}, {"name": "analysts"}, None),
    ("DELETE", "/management/v1/projects/{project_id}/roles/{role_id}"): (_ROLE_PARAMS, None, "role"),
    ("PUT", "/management/v1/projects/{project_id}/roles/{role_id}/assignees"): (_ROLE_PARAMS, {"user": f"dex~{READER}"}, "role"),
    ("DELETE", "/management/v1/projects/{project_id}/roles/{role_id}/assignees"): (_ROLE_PARAMS, {"user": f"dex~{READER}"}, "assignee"),
    ("POST", "/management/v1/warehouse/{id}/access/grant"): ({"id": WAREHOUSE}, {"user": "dex~erin", "relation": "reader"}, None),
    ("POST", "/management/v1/warehouse/{id}/access/revoke"): ({"id": WAREHOUSE}, {"user": "dex~erin", "relation": "reader"}, "reader"),
    ("POST", "/management/v1/warehouse/{id}/managed-access/set"): ({"id": WAREHOUSE}, {"enabled": True}, None),
}
#: The tenancy doors that write no tuple on a creation once their gate admitted the caller, and why.
_WRITING_NO_TUPLE = {
    ("POST", "/management/v1/warehouses"),  # makes a creation, ordered by its own protocol (ADR 0245 decision 3)
    ("DELETE", "/management/v1/projects/{project_id}"),  # ends a creation, by its own mark (review T2, T3)
    ("DELETE", "/management/v1/warehouses/{warehouse_id}"),
    ("POST", "/management/v1/projects/{project_id}/protection"),  # registry writes, each conditional on the creation
    ("POST", "/management/v1/warehouses/{warehouse_id}/protection"),
    ("POST", "/management/v1/warehouses/{warehouse_id}/activate"),
    ("POST", "/management/v1/warehouses/{warehouse_id}/deactivate"),
    ("POST", "/management/v1/warehouses/{warehouse_id}/validate"),
    ("POST", "/management/v1/project/{id}/policy/set"),
    ("POST", "/management/v1/project/{id}/policy/delete"),
    ("POST", "/management/v1/projects/{id}/labels/define"),
    ("POST", "/management/v1/projects/{id}/labels/{name}/delete"),
    ("POST", "/management/v1/projects/{id}/access/list"),  # reads, sent as POST
    ("POST", "/management/v1/projects/{id}/access/my-permissions"),
    ("POST", "/management/v1/project/{id}/policy/describe"),
    ("POST", "/management/v1/warehouse/{id}/access/list"),
    ("POST", "/management/v1/warehouse/{id}/access/my-permissions"),
    ("POST", "/management/v1/warehouse/{id}/managed-access/describe"),
}


def test_every_tenancy_door_that_writes_a_tuple_is_in_the_after_write_roster() -> None:
    changing = {(method, route) for method, route in _left_to_handlers() if _TENANCY_DOOR.match(route) and method != "GET"}
    assert (changing - set(_WRITING) - _WRITING_NO_TUPLE, set(_WRITING) - changing) == (set(), set()), (
        "tenancy doors in neither roster, and roster entries naming no such route"
    )


def _made_again(estate: Any, kind: str, object_id: str) -> None:
    """The record of ``kind`` ``object_id`` replaced by another creation, as a delete and a create landing between a door's
    Write and its answer leave it."""
    from catalog.services import projects as project_registry
    from catalog.services.control_records import write_json
    from lakehouse.governed import tenancy as named

    settings = get_settings()
    read = project_registry.get_project_record if kind == "project" else warehouses.get_warehouse_record
    record = read(settings.registry_root, settings.storage_options(), object_id) or {}
    key = project_registry.record_key(object_id) if kind == "project" else warehouses.record_key(object_id)
    write_json(settings.registry_root, settings.storage_options(), key, {**record, "uuid": named.new_uuid()})


@pytest.mark.parametrize("door", sorted(_WRITING), ids=lambda key: f"{key[0]} {key[1]}")
def test_a_tenancy_door_whose_creation_ends_under_its_write_answers_the_deleted_object(
    estate: Any, monkeypatch: pytest.MonkeyPatch, door: tuple[str, str]
) -> None:
    """Review T6: a door that wrote a tuple on a project's or a warehouse's creation resolves the id again once the Write
    landed, so where the id names another creation by then (the object deleted and made again meanwhile) the door answers
    the deleted object's 404, never success for a change on an object that is gone."""
    from lakehouse.governed import fga

    method, route = door
    params, body, needs = _WRITING[door]
    client, alice = _as_a_caller_sees_it(estate.client), estate.bearer("alice")
    if needs is not None:
        made = client.post(f"/management/v1/projects/{PROJECT}/roles", json={"name": "curators"}, headers=alice)
        assert made.status_code == 201, made.text
        role = made.json()["id"]
        params = {name: value.replace("{role}", role) for name, value in params.items()}
        grant = {
            "member": ("PUT", f"/management/v1/projects/{PROJECT}/members"),
            "decider": ("POST", f"/management/v1/projects/{PROJECT}/access/grant"),
            "reader": ("POST", f"/management/v1/warehouse/{WAREHOUSE}/access/grant"),
            "assignee": ("PUT", f"/management/v1/projects/{PROJECT}/roles/{role}/assignees"),
        }.get(needs)
        if grant is not None:
            _ok(client.request(grant[0], grant[1], json=body, headers=alice))
    kind, object_id = ("warehouse", WAREHOUSE) if WAREHOUSE in params.values() else ("project", PROJECT)
    real, armed = fga.require_named_by_creation, [True]

    def _write_then_made_again(tuples: Any) -> None:  # noqa: ANN401 — the Write's own tuples
        real(tuples)
        if armed:
            armed.clear()
            _made_again(estate, kind, object_id)

    monkeypatch.setattr(fga, "require_named_by_creation", _write_then_made_again)

    answered = _Refusing(method=method, route=route, params=params, body=body, caller="alice").send(client, alice)

    assert (armed, _refusal(answered)) == ([], _NOT_FOUND), answered.text
