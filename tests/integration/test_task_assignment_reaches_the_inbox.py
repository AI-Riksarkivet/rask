"""The assignment lane end to end, across BOTH services' real code and no mock of either.

The unit suites prove each half in isolation: `tests/unit/test_task_endpoints.py` shows the annotator
emits an event naming the assignee, and `services/notifications/tests/test_control_events.py` shows the
ingress delivers such an event to that person. Neither proves they AGREE — and a wire contract between
two deployables is precisely where agreement is assumed and not checked.

That gap is not hypothetical here. This lane needs FOUR independent facts to line up, spread across
three packages, and each has a silent failure mode:

* the action string the annotator sends is in the notifications lane's ``NAMED_ACTIONS`` — otherwise the
  event is filed IGNORED and **acked as SUCCESS**, so nothing anywhere reports the loss;
* that same string is a ``NotificationReason`` member — otherwise ``as_delivery`` raises
  ``ValueError`` on EVERY delivery, at runtime, on an event that has already been accepted;
* the object type validates against ``ControlObjectType`` — otherwise the envelope is rejected at the
  door as unparseable and DROPped;
* ``extra.subject`` is spelled as a principal the ingress can turn into an inbox address — a userset or
  the ``user:*`` wildcard names nobody.

So this drives the annotator's real HTTP route with a recording emitter, takes the event object it
actually produced, serializes it the way the Dapr transport does (``model_dump_json``), and feeds THAT
through the notifications ingress. The only thing standing in for infrastructure is the inbox actor.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.dependencies import get_control_emitter
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import ProjectState, TaskState
from notifications.api.control_events import ingest_control_event
from notifications.models import NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.control_events import CatalogControlEvent
from service_kit.exceptions import register_handlers


MANAGER = "alice"
ASSIGNEE = "bob"
PROJECT_ID = "proj-e2e"


class _RecordingControl:
    """The Dapr publisher's seat. Holds the event objects the route actually emitted."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


class _TaskActor:
    """The task's Dapr actor. Returns a canned pre-turn snapshot and accepts the transition."""

    def __init__(self, state: TaskState, assignee: str | None = None) -> None:
        self._task: dict[str, Any] = {
            "state": state,
            "assignee": assignee,
            "submitted_by": None,
            "task_id": "t-e2e",
            "project_id": PROJECT_ID,
        }

    async def get(self) -> dict[str, Any]:
        return dict(self._task)

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**self._task, "state": TaskState.CLAIMED, "assignee": payload.get("assignee")}


class _ProjectActor:
    async def get(self) -> dict[str, Any]:
        return {"state": str(ProjectState.LABELING), "project_id": PROJECT_ID, "consensus_n": 1}


def _opener(boxes: dict[str, list[dict[str, Any]]]) -> Any:
    """An `InboxOpener` over `boxes`.

    `cast`, not a suppression: `_Inbox` satisfies the proxy STRUCTURALLY (the ingress only ever calls
    `deliver`), but `TypedActorProxy` dispatches over the wire and cannot be subclassed usefully in a
    test. Naming the real type keeps the seam honest — the same reason `_proxy` casts in the service.
    """
    return lambda subject: cast(TypedActorProxy, _Inbox(boxes, subject))


class _Inbox:
    """One subject's inbox actor, idempotent on `notification_id` exactly as the real one is."""

    def __init__(self, boxes: dict[str, list[dict[str, Any]]], subject: str) -> None:
        self._boxes, self._subject = boxes, subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False}
        rows.append(payload)
        return {"delivered": True}


@pytest.fixture
def annotator_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _RecordingControl, _TaskActor]:
    actor = _TaskActor(TaskState.UNASSIGNED)
    control = _RecordingControl()

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        del user, relation, obj  # every gate is open here; this suite is about the WIRE, not authz
        return True

    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    from annotator.api.v1.endpoints import project_events as pe

    monkeypatch.setattr(pe, "_project_proxy", lambda _p: _ProjectActor())

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: MANAGER
    app.dependency_overrides[get_control_emitter] = lambda: control
    return app, control, actor


@pytest.mark.asyncio
async def test_an_assignment_travels_from_the_annotator_route_into_the_assignees_inbox(
    annotator_app: tuple[FastAPI, _RecordingControl, _TaskActor],
) -> None:
    """The whole lane: alice assigns bob, and the row lands in BOB's inbox and nowhere else."""
    app, control, _actor = annotator_app

    response = TestClient(app).post("/tasks/t-e2e/events", json={"event": "assign", "assignee": ASSIGNEE})
    assert response.status_code == 200
    assert len(control.events) == 1, "the assign edge must emit exactly one control event"

    # Over the wire as the Dapr transport really sends it — `model_dump_json`, then parsed back by the
    # consumer. A field that only survives in-process (an enum, a datetime) fails HERE rather than in
    # a cluster, which is the entire reason this crosses the serialization boundary.
    wire = json.loads(control.events[0].model_dump_json())

    boxes: dict[str, list[dict[str, Any]]] = {}
    result = await ingest_control_event(wire, open_inbox=_opener(boxes))

    assert result == {"status": "SUCCESS"}
    assert list(boxes) == [ASSIGNEE], "the manager who assigned must not be told about their own click"
    row = boxes[ASSIGNEE][0]
    assert row["reason"] == NotificationReason.TASK_ASSIGNED
    assert row["object_id"] == "annotation_task:t-e2e"


@pytest.mark.asyncio
async def test_a_release_travels_to_the_holder_who_lost_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror. `release` is the unassign, and its audience is the CURRENT holder — read off the
    task's pre-turn snapshot, because the request body names nobody on that edge."""
    actor = _TaskActor(TaskState.CLAIMED, assignee=ASSIGNEE)
    control = _RecordingControl()

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        del user, relation, obj  # every gate is open here; this suite is about the WIRE, not authz
        return True

    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: actor)
    from annotator.api.v1.endpoints import project_events as pe

    monkeypatch.setattr(pe, "_project_proxy", lambda _p: _ProjectActor())

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: MANAGER
    app.dependency_overrides[get_control_emitter] = lambda: control

    assert TestClient(app).post("/tasks/t-e2e/events", json={"event": "release"}).status_code == 200

    boxes: dict[str, list[dict[str, Any]]] = {}
    wire = json.loads(control.events[0].model_dump_json())
    assert await ingest_control_event(wire, open_inbox=_opener(boxes)) == {"status": "SUCCESS"}
    assert boxes[ASSIGNEE][0]["reason"] == NotificationReason.TASK_UNASSIGNED


@pytest.mark.asyncio
async def test_a_redelivery_lands_exactly_one_row(
    annotator_app: tuple[FastAPI, _RecordingControl, _TaskActor],
) -> None:
    """Dapr redelivers at-least-once, so the SAME envelope arrives twice. The inbox is idempotent on
    `notification_id`, which for a control event is `<event_id>@<ACTION>` — and `event_id` is stamped
    once at construction, so a redelivery carries the id the first attempt did."""
    app, control, _actor = annotator_app
    TestClient(app).post("/tasks/t-e2e/events", json={"event": "assign", "assignee": ASSIGNEE})
    wire = json.loads(control.events[0].model_dump_json())

    boxes: dict[str, list[dict[str, Any]]] = {}
    opener = _opener(boxes)
    assert await ingest_control_event(wire, open_inbox=opener) == {"status": "SUCCESS"}
    assert await ingest_control_event(wire, open_inbox=opener) == {"status": "SUCCESS"}

    assert len(boxes[ASSIGNEE]) == 1, "an at-least-once redelivery must not double the badge"
