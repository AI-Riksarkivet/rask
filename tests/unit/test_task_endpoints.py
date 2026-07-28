"""Task endpoints — the authorization surface of the CVAT/Label-Studio loop.

The actor is faked so these prove the HTTP contract without a sidecar: which `can_*` each event
demands, and the two rules the transition table cannot express because they depend on the task's own
rows — no self-review, and only the lease holder may save or submit.

The permission is NOT asserted per-route by hand. `TASK_EDGES` carries it on the edge, so these tests
read the expectation from the machine — meaning a new edge is gated automatically and a route cannot
quietly drift from the model.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.machines import TASK_EDGES
from annotator.projects.models import TaskState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


SUBJECT = "gina"


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


def _app(actor: _FakeActor, *, allow: bool, seen: list[dict[str, Any]] | None = None) -> FastAPI:
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
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------------------------------
# The permission comes from the machine, not from the route
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "event"),
    [(s, e) for (s, e) in TASK_EDGES if TASK_EDGES[(s, e)][1] is not None],
)
def test_every_edge_checks_the_permission_the_table_declares(state: TaskState, event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the expectation from TASK_EDGES: adding an edge gates it with no route change."""
    expected = TASK_EDGES[(state, event)][1]
    actor = _FakeActor(_task(state=state))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(actor, allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": event, "project": "acme"})

    assert r.status_code == 200, r.text
    assert seen, f"{event} from {state} was not authorized at all"
    assert seen[0]["relation"] == expected
    assert seen[0]["obj"] == "project:acme", "the check must target the tenant"
    assert seen[0]["user"] == SUBJECT, "the check must use the VERIFIED subject"


def test_a_system_caused_edge_needs_no_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lease_expired` has permission None — no principal fires it, so nothing is checked."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(actor, allow=True, seen=seen))

    r = client.post("/tasks/t1/events", json={"event": "lease_expired", "project": "acme"})
    assert r.status_code == 200
    assert seen == [], "a system-caused edge performed an authorization check"


def test_a_denied_check_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task())
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=False))
    r = client.post("/tasks/t1/events", json={"event": "claim", "project": "acme"})
    assert r.status_code == 403
    assert actor.fired == [], "the actor was invoked despite the check failing"


def test_an_edge_absent_from_the_table_is_409_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.UNASSIGNED))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))
    r = client.post("/tasks/t1/events", json={"event": "accept", "project": "acme"})
    assert r.status_code == 409


# --------------------------------------------------------------------------------------------------
# The two rules the table cannot express
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["accept", "fix_and_accept", "request_changes"])
def test_a_reviewer_may_not_review_their_own_submission(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2. `can_review` is not enough — the submitter is excluded even when they hold it."""
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))

    r = client.post("/tasks/t1/events", json={"event": event, "project": "acme"})

    assert r.status_code == 403
    assert "own submission" in r.text
    assert actor.fired == []


def test_someone_elses_submission_can_be_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.IN_REVIEW, submitted_by="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))
    assert client.post("/tasks/t1/events", json={"event": "accept", "project": "acme"}).status_code == 200


@pytest.mark.parametrize("event", ["save_draft", "submit"])
def test_only_the_lease_holder_may_save_or_submit(event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_annotate` says you may annotate in this project; the claim says you may annotate THIS
    task right now. Holding the permission is not holding the task."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))

    r = client.post("/tasks/t1/events", json={"event": event, "project": "acme"})

    assert r.status_code == 403
    assert "held by dave" in r.text


def test_the_draft_save_records_the_verified_subject_as_author(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance: who annotated is the VERIFIED subject, never a field the client supplied."""
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee=SUBJECT))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))

    r = client.put(
        "/tasks/t1/draft",
        json={"project": "acme", "project_id": "p1", "shapes": [{"shape_type": "bbox"}]},
    )

    assert r.status_code == 200, r.text
    assert actor.drafts[0]["author"] == SUBJECT


def test_a_draft_save_by_a_non_holder_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(_task(state=TaskState.CLAIMED, assignee="dave"))
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))
    r = client.put("/tasks/t1/draft", json={"project": "acme", "project_id": "p1", "shapes": []})
    assert r.status_code == 403
    assert actor.drafts == []


def test_an_unsent_task_is_409_rather_than_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeActor(None)
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    client = TestClient(_app(actor, allow=True))
    assert client.post("/tasks/t1/events", json={"event": "claim", "project": "acme"}).status_code == 409
