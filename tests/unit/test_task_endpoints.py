"""Task endpoints — the authorization surface of the CVAT/Label-Studio loop.

The actor is faked so these prove the HTTP contract without a sidecar: which `can_*` each event
demands, on WHICH object, and the rules the transition table cannot express because they depend on
the task's own rows.

The permission is not asserted per-route by hand. `TASK_EDGES` carries it on the edge, so these read
the expectation from the machine — a new edge is gated automatically and a route cannot quietly drift
from the model.

Three of these exist because an adversarial review found the corresponding defect in the first
version of this file's subject, and the ORIGINAL tests asserted the buggy behaviour as correct:

- the check ran on `project:<tenant>`, a type on which `model.fga` defines none of these relations;
- `review_required` was a request field, so an annotator could submit with review waived;
- system-only edges were exposed unauthenticated, because "no permission required" was read as
  "no permission checked".
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.machines import TASK_EDGES
from annotator.projects.models import ProjectState, TaskState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


SUBJECT = "gina"
PROJECT_ID = "proj-1"


class _FakeActor:
    """Stands in for the Dapr actor proxy: records what it was asked, returns canned state."""

    def __init__(self, state: dict[str, Any] | None) -> None:
        self.state = state
        self.fired: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []

    async def get(self) -> dict[str, Any] | None:
        return self.state

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.fired.append(payload)
        return {**(self.state or {}), "state": "claimed"}

    async def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.drafts.append(payload)
        return {"revision": 1, "shapes": payload.get("shapes", [])}

    async def get_draft(self) -> dict[str, Any] | None:
        return {"revision": 1, "shapes": []}


def _app(*, allow: bool, seen: list[dict[str, Any]] | None = None) -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    return app


def _task(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": TaskState.UNASSIGNED,
        "assignee": None,
        "submitted_by": None,
        "task_id": "t1",
        "project_id": PROJECT_ID,
    }
    base.update(kw)
    return base


class _FakeProjectActor:
    """The project the task belongs to. Read on every task event to enforce §5.2 rule 5."""

    def __init__(self, state: ProjectState = ProjectState.LABELING) -> None:
        self.state = state
        self.consensus_n = 1

    async def get(self) -> dict[str, Any]:
        return {"state": str(self.state), "project_id": PROJECT_ID, "consensus_n": self.consensus_n}


@pytest.fixture(autouse=True)
def _live_project(monkeypatch: pytest.MonkeyPatch) -> _FakeProjectActor:
    """Every task event now reads its project's state (§5.2 rule 5 — nothing escapes a published
    project). Autouse so each test gets a LABELING project by default; the freeze tests mutate it."""
    from annotator.api.v1.endpoints import project_events as pe

    project = _FakeProjectActor()
    monkeypatch.setattr(pe, "_project_proxy", lambda _p: project)
    return project


#: Edges a principal may fire — everything with a declared permission.
PRINCIPAL_EDGES = [(s, e) for (s, e) in TASK_EDGES if TASK_EDGES[(s, e)][1] is not None]


# --------------------------------------------------------------------------------------------------
# The permission — and the object — come from the model, not from the route
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "event"), PRINCIPAL_EDGES)
def test_every_edge_checks_the_permission_the_table_declares(state: TaskState, event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = TASK_EDGES[(state, event)][1]
    actor = _FakeActor(_task(state=state))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 200, r.text
    assert seen, f"{event} from {state} was not authorized at all"
    assert seen[0]["relation"] == expected
    assert seen[0]["user"] == SUBJECT, "the check must use the VERIFIED subject"


@pytest.mark.parametrize(("state", "event"), PRINCIPAL_EDGES)
def test_every_check_targets_the_annotation_project_not_the_tenant(state: TaskState, event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`model.fga` defines can_claim/can_annotate/can_review/can_manage on `annotation_project`.
    Checking them on `project:<tenant>` asks OpenFGA for a relation that type does not define — it
    fails closed, so the entire task plane 403s the moment FGA is enabled. This file asserted the
    wrong object until an adversarial review caught it."""
    actor = _FakeActor(_task(state=state))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    client.post("/tasks/t1/events", json={"event": event})

    assert seen[0]["obj"] == f"annotation_project:{PROJECT_ID}"


def test_the_authorization_object_cannot_be_chosen_by_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """The project id is read from the TASK's own record. A body field naming another project must
    not change which object the permission is evaluated against."""
    actor = _FakeActor(_task(project_id=PROJECT_ID))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    client.post("/tasks/t1/events", json={"event": "claim", "project_id": "someone-elses", "project": "other-tenant"})

    assert seen[0]["obj"] == f"annotation_project:{PROJECT_ID}"


# --------------------------------------------------------------------------------------------------
# System-only edges are not a public surface
# --------------------------------------------------------------------------------------------------


def test_a_system_only_edge_is_refused_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lease_expired` has permission `None` because no PRINCIPAL fires it — the actor's reminder
    does. Treating "no permission required" as "no permission checked" exposed it unauthenticated,
    letting anyone strip another annotator's claim. It is refused outright."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": "lease_expired"})

    assert r.status_code == 403
    assert actor.fired == [], "a system-only edge reached the actor over HTTP"


def test_every_permissionless_edge_is_in_the_refused_set() -> None:
    """Derived from the machine, so a new `None`-permission edge is refused without a code change."""
    from annotator.api.v1.endpoints.tasks import SYSTEM_ONLY_EVENTS

    assert {e for (_s, e), (_t, p) in TASK_EDGES.items() if p is None} == SYSTEM_ONLY_EVENTS
    assert "lease_expired" in SYSTEM_ONLY_EVENTS


# --------------------------------------------------------------------------------------------------
# review_required is not a request field
# --------------------------------------------------------------------------------------------------


def test_an_annotator_cannot_waive_review_from_the_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this replaces: `review_required` was a `FireRequest` field, so a submitter could
    pass `false` and land straight in `accepted` — self-approving past the one guarantee the whole
    plane exists to provide. It is captured on the task at send time and ignored here."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "submit", "review_required": False})

    assert r.status_code == 200, r.text
    assert "review_required" not in actor.fired[0], "the request body reached the actor's submit decision"


def test_a_denied_check_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task())
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=False))
    r = client.post("/tasks/t1/events", json={"event": "claim"})
    assert r.status_code == 403
    assert actor.fired == [], "the actor was invoked despite the check failing"


def test_an_edge_absent_from_the_table_is_409_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.UNASSIGNED))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "accept"}).status_code == 409


# --------------------------------------------------------------------------------------------------
# The rules the table cannot express
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["accept", "fix_and_accept", "request_changes"])
def test_a_reviewer_may_not_review_their_own_submission(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2. `can_review` is not enough — the submitter is excluded even when they hold it."""
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert "own submission" in r.text
    assert actor.fired == []


def test_someone_elses_submission_can_be_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "accept"}).status_code == 200


def test_only_the_lease_holder_may_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_annotate` says you may annotate in this project; the claim says you may annotate THIS
    task right now. Holding the permission is not holding the task."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "submit"})

    assert r.status_code == 403
    assert "held by dave" in r.text


# --------------------------------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------------------------------


def test_the_draft_save_records_the_verified_subject_as_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance: who annotated is the VERIFIED subject, never a field the client supplied."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 200, r.text
    assert actor.drafts[0]["author"] == SUBJECT
    assert actor.drafts[0]["project_id"] == PROJECT_ID, "the project id must come from the task, not the body"


@pytest.mark.parametrize("state", [TaskState.ACCEPTED, TaskState.IN_REVIEW, TaskState.SKIPPED, TaskState.UNASSIGNED])
def test_a_draft_is_only_writable_while_the_task_is_claimed(state: TaskState, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this replaces: `save_draft` consulted the assignee but never the STATE, so an
    ACCEPTED task's shapes could be rewritten after review — and during a publish — putting
    annotations into the lakehouse no reviewer ever saw. `TASK_EDGES` already says `save_draft` is
    legal only from CLAIMED; the route now honours the machine instead of writing around it."""
    actor = _FakeActor(_task(state=state, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 409, r.text
    assert actor.drafts == [], f"a draft was written to a {state} task"


def test_a_draft_save_by_a_non_holder_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    r = client.put("/tasks/t1/draft", json={"shapes": []})
    assert r.status_code == 403
    assert actor.drafts == []


def test_an_unsent_task_is_409_rather_than_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(None)
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))
    assert client.post("/tasks/t1/events", json={"event": "claim"}).status_code == 409


def test_reading_a_task_authorizes_before_returning_it(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task())
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=False, seen=seen))

    r = client.get("/tasks/t1")

    assert r.status_code == 403
    assert seen[0] == {"user": SUBJECT, "relation": "can_view", "obj": f"annotation_project:{PROJECT_ID}"}


# --------------------------------------------------------------------------------------------------
# §5.2 rule 5 — nothing escapes a published project
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("frozen", [ProjectState.PUBLISHING, ProjectState.PUBLISHED, ProjectState.ARCHIVED])
def test_no_task_transition_survives_a_published_project(frozen: ProjectState, _live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance is frozen with the artifact. A task that moved after the publish would describe a
    dataset that no longer matches it — and during `publishing` it could change the very rows the
    saga is mid-way through reading. The rule was documented and enforced nowhere."""
    _live_project.state = frozen
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "accept"})

    assert r.status_code == 409, r.text
    assert "frozen with the published artifact" in r.text
    assert actor.fired == []


def test_a_draft_cannot_be_saved_into_a_publishing_project(_live_project: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharpest case: the saga is reading drafts right now to build the plan."""
    _live_project.state = ProjectState.PUBLISHING
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.put("/tasks/t1/draft", json={"shapes": [{"shape_type": "bbox"}]})

    assert r.status_code == 409
    assert actor.drafts == []


# --------------------------------------------------------------------------------------------------
# §5.2 rule 2 — only the lease holder writes, "even a manager"
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["submit", "skip"])
def test_skip_and_submit_are_lease_holder_only(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`skip` was missing from this check, so any annotator could discard work someone else was
    holding — and with `requeue_for_others`, consume their turn at it."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": event})

    assert r.status_code == 403
    assert "held by dave" in r.text
    assert actor.fired == []


def test_release_is_refused_to_a_non_holder_without_can_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2: `release` is "lease holder OR can_manage". Gating it on plain `can_annotate` let any
    annotator break another's claim."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        seen.append({"relation": relation})
        return relation == "can_annotate"  # holds the rung, but is not a manager

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    r = TestClient(app).post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 403
    assert "needs can_manage" in r.text
    assert {"relation": "can_manage"} in seen, "the manager escape hatch was never consulted"


def test_a_manager_may_release_a_task_held_by_someone_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented escape hatch for a task pinned to someone unavailable."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))  # holds everything, including can_manage

    r = client.post("/tasks/t1/events", json={"event": "release"})

    assert r.status_code == 200, r.text
    assert actor.fired[0]["event"] == "release"


# --------------------------------------------------------------------------------------------------
# assign names a recipient
# --------------------------------------------------------------------------------------------------


def test_a_manager_assigns_to_a_NAMED_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this replaces made `assign` set the assignee to the manager who fired it, so a manager
    could only ever assign to themselves — silently turning the one manager-driven distribution
    mechanism in the whole plane into a self-claim."""
    actor = _FakeActor(_task(state=TaskState.UNASSIGNED))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(allow=True))

    r = client.post("/tasks/t1/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 200, r.text
    assert actor.fired[0]["assignee"] == "dave"
    assert actor.fired[0]["actor"] == SUBJECT, "the manager is still recorded as the actor"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — one replica per annotator per group
# --------------------------------------------------------------------------------------------------


def test_claiming_a_second_replica_of_the_same_group_is_409(monkeypatch: pytest.MonkeyPatch, _live_project: _FakeProjectActor) -> None:
    """Independence is the point of consensus: the same person labeling two replicas of one item is
    one opinion counted twice. Enforced server-side at claim — sibling ids are deterministic
    (`{gid}-r{k}`), so the guard reads the siblings' own actors and refuses with the rule NAMED."""
    _live_project.consensus_n = 2
    docs = {
        "g1-r1": _task(task_id="g1-r1", state=TaskState.CLAIMED, assignee=SUBJECT, replica_of="g1"),
        "g1-r2": _task(task_id="g1-r2", replica_of="g1"),
    }

    class _PerTask(_FakeActor):
        def __init__(self, task_id: str) -> None:
            super().__init__(docs.get(task_id))

        async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the guard must refuse BEFORE the actor fires")

    monkeypatch.setattr(tasks_ep, "_proxy", _PerTask)

    client = TestClient(_app(allow=True))
    r = client.post("/tasks/g1-r2/events", json={"event": "claim"})

    assert r.status_code == 409, r.text
    assert "one replica per annotator" in r.json()["detail"]
    assert "g1-r1" in r.json()["detail"], "the refusal must name the replica already held"


def test_assigning_a_second_replica_to_the_same_recipient_is_409(monkeypatch: pytest.MonkeyPatch, _live_project: _FakeProjectActor) -> None:
    """The assign edge is guarded on the RECIPIENT (payload.assignee), not the manager firing it —
    a manager distributing two replicas of one item to the same annotator defeats independence
    exactly like a double claim. The sibling here was already WORKED (submitted_by) with the lease
    long released, which is precisely the state a naive assignee-only check would miss."""
    _live_project.consensus_n = 2
    docs = {
        "g1-r1": _task(task_id="g1-r1", state=TaskState.IN_REVIEW, submitted_by="dave", replica_of="g1"),
        "g1-r2": _task(task_id="g1-r2", replica_of="g1"),
    }

    class _PerTask(_FakeActor):
        def __init__(self, task_id: str) -> None:
            super().__init__(docs.get(task_id))

        async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the guard must refuse BEFORE the actor fires")

    monkeypatch.setattr(tasks_ep, "_proxy", _PerTask)

    client = TestClient(_app(allow=True))
    r = client.post("/tasks/g1-r2/events", json={"event": "assign", "assignee": "dave"})

    assert r.status_code == 409, r.text
    assert "one replica per annotator" in r.json()["detail"]
    assert "g1-r1" in r.json()["detail"], "the refusal must name the replica already worked"
