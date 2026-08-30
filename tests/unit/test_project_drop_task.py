"""`DELETE /projects/{id}/tasks/{task_id}` — an item that can never be completed must have an exit.

A send can put items into a project that NOBODY can finish. The case that motivated this is an item
naming a media dataset that has since been renamed or removed: the canvas cannot open it, so it
cannot be claimed, submitted or skipped. The publish precondition requires EVERY task terminal, so
one unfinishable item wedges the project permanently — and the only way past it was to abandon the
project and re-send everything.

The same shape as the adjudication clear beside it, and for the same audit-found reason: a state
with no exit is a state that eventually traps someone.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as events_ep
from annotator.api.v1.endpoints.project_events import router
from service_kit.exceptions import register_handlers


class _FakeProjectActor:
    """Holds an index the way the real actor does, so removal is observable."""

    def __init__(self, state: str = "labeling", index: dict[str, str] | None = None) -> None:
        self._state = state
        self.index = index if index is not None else {"t1": "unassigned", "t2": "accepted"}
        self.dropped: list[str] = []

    async def get(self) -> dict[str, Any] | None:
        return {"project_id": "p1", "tenant": "acme", "slug": "vasa", "state": self._state}

    async def drop_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload["task_id"])
        self.dropped.append(task_id)
        if task_id not in self.index:
            return {"task_id": task_id, "removed": False, "total": len(self.index)}
        del self.index[task_id]
        return {"task_id": task_id, "removed": True, "total": len(self.index)}


def _app(actor: _FakeProjectActor, monkeypatch: pytest.MonkeyPatch, *, allow: bool = True, seen: list | None = None) -> FastAPI:
    monkeypatch.setattr(events_ep, "_project_proxy", lambda _p: actor)
    # The route reads the TASK before dropping it, to learn who loses the work. These tests are about
    # the removal itself, so the default stand-in holds nobody — the audience cases override it.
    monkeypatch.setattr(events_ep, "_task_proxy", lambda _t: _FakeTaskActor(None))

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    return app


def test_a_manager_drops_an_unfinishable_item(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor()
    client = TestClient(_app(actor, monkeypatch))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert "t1" not in actor.index, "the route answered 200 without removing anything"
    assert "t2" in actor.index, "dropping one item took another with it"


def test_the_gate_is_can_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discarding someone's queued work is a manager act, not an annotator's."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(_FakeProjectActor(), monkeypatch, seen=seen))

    client.delete("/projects/p1/tasks/t1")

    assert [c["relation"] for c in seen] == ["can_manage"]
    assert seen[0]["obj"] == "annotation_project:p1"


def test_a_denied_caller_gets_403_and_removes_NOTHING(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor()
    client = TestClient(_app(actor, monkeypatch, allow=False))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 403, r.text
    assert actor.dropped == [], "a refused caller still reached the actor"
    assert "t1" in actor.index


def test_dropping_an_ABSENT_task_is_a_no_op_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent, so a retry cannot fail against a project that is already how the caller wants it."""
    actor = _FakeProjectActor()
    client = TestClient(_app(actor, monkeypatch))

    r = client.delete("/projects/p1/tasks/never-existed")

    assert r.status_code == 200, r.text
    assert r.json()["removed"] is False


@pytest.mark.parametrize("state", ["draft", "labeling"])
def test_droppable_while_the_project_can_still_change_what_it_publishes(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    actor = _FakeProjectActor(state)
    client = TestClient(_app(actor, monkeypatch))

    assert client.delete("/projects/p1/tasks/t1").status_code == 200


@pytest.mark.parametrize("state", ["frozen", "publishing", "published", "archived"])
def test_refused_once_provenance_is_being_prepared(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past `frozen` the answer set is closed and a publish is being prepared against it. Removing an
    item then would change what the run facet describes after the description was fixed."""
    actor = _FakeProjectActor(state)
    client = TestClient(_app(actor, monkeypatch))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 409, r.text
    assert state in r.text, "the refusal must name the state that caused it"
    assert actor.dropped == []


def test_an_unknown_project_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Missing(_FakeProjectActor):
        async def get(self) -> dict[str, Any] | None:
            return None

    actor = _Missing()
    client = TestClient(_app(actor, monkeypatch))

    assert client.delete("/projects/p1/tasks/t1").status_code == 404
    assert actor.dropped == []


# --------------------------------------------------------------------------------------------------
# The person holding the dropped item is TOLD
# --------------------------------------------------------------------------------------------------
#
# `drop_task` is the sharpest of the annotator's departure edges: a manager discards an item somebody
# may be actively holding, the task actor is left alive with their draft in it, and the index entry
# that `saga.collect` enumerates is gone — so their work is not merely stopped, it is orphaned where
# nothing will ever look for it again. Before this the only record was one audit line in a log they
# cannot read.


class _RecordingControl:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _FakeTaskActor:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self._doc = doc

    async def get(self) -> dict[str, Any] | None:
        return self._doc


def _app_with_control(
    actor: _FakeProjectActor,
    control: _RecordingControl,
    monkeypatch: pytest.MonkeyPatch,
    task_doc: dict[str, Any] | None,
) -> FastAPI:
    from annotator.api.dependencies import get_control_emitter

    app = _app(actor, monkeypatch)
    monkeypatch.setattr(events_ep, "_task_proxy", lambda _t: _FakeTaskActor(task_doc))
    app.dependency_overrides[get_control_emitter] = lambda: control
    return app


def test_dropping_a_HELD_task_tells_the_person_who_loses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The holder's draft is orphaned by this call. `assignee` is read from the task BEFORE the drop —
    afterwards the index entry naming it is gone."""
    actor, control = _FakeProjectActor(), _RecordingControl()
    client = TestClient(_app_with_control(actor, control, monkeypatch, {"assignee": "dave", "submitted_by": None}))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 200, r.text
    assert [(e.action, e.extra["subject"]) for e in control.events] == [("task_dropped", "user:dave")]


def test_dropping_a_SUBMITTED_task_tells_whoever_did_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task in review has no assignee — the actor nulls it on submit — so the person who loses work
    is the SUBMITTER. Falling back only to `assignee` would tell nobody for exactly the tasks where
    the most work has already been done."""
    actor, control = _FakeProjectActor(), _RecordingControl()
    client = TestClient(_app_with_control(actor, control, monkeypatch, {"assignee": None, "submitted_by": "dave"}))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 200, r.text
    assert [(e.action, e.extra["subject"]) for e in control.events] == [("task_dropped", "user:dave")]


def test_dropping_an_ABSENT_task_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route is idempotent by design, so a retry must not put a second row in anyone's inbox — and
    a drop that removed nothing is not a change to announce."""
    actor, control = _FakeProjectActor(), _RecordingControl()
    client = TestClient(_app_with_control(actor, control, monkeypatch, {"assignee": "dave", "submitted_by": None}))

    r = client.delete("/projects/p1/tasks/never-sent")

    assert r.status_code == 200, r.text
    assert control.events == [], "nothing was removed, so nothing happened to announce"


def test_dropping_a_task_nobody_holds_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The motivating case: an unfinishable item nobody ever claimed. There is no audience, and the
    manager doing the tidying is looking at the response."""
    actor, control = _FakeProjectActor(), _RecordingControl()
    client = TestClient(_app_with_control(actor, control, monkeypatch, {"assignee": None, "submitted_by": None}))

    r = client.delete("/projects/p1/tasks/t1")

    assert r.status_code == 200, r.text
    assert control.events == []
