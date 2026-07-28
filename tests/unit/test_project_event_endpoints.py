"""Project lifecycle endpoints — and the two-door publish crossing of §6.2.

The publish doors are the design's most important authz consequence, so most of this file is one
question asked several ways: can anyone move labels into the lakehouse while holding only half the
authority? `can_publish` alone must not be enough, and `can_create_table` alone must not be enough.
Collapsing the two would let either plane's admin quietly acquire the other's.

The actors are faked, so these prove the HTTP contract without a sidecar.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as ev
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import ProjectState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


SUBJECT = "henry"


class _FakeProject:
    def __init__(self, state: ProjectState | None = ProjectState.FROZEN) -> None:
        self.state = state
        self.fired: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.raise_on_fire: Exception | None = None

    async def get(self) -> dict[str, Any] | None:
        return None if self.state is None else {"state": str(self.state), "project_id": "p1"}

    async def fire(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.raise_on_fire:
            raise self.raise_on_fire
        self.fired.append(payload)
        return {"state": "publishing", "project_id": "p1"}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {"task_id": payload["task_id"], "created": True, "counts": {}}

    async def list_tasks(self) -> dict[str, Any]:
        return {"tasks": {}, "counts": {}, "total": 0, "terminal": 0, "may_publish": True}


class _FakeTask:
    def __init__(self) -> None:
        self.seeded: list[dict[str, Any]] = []

    async def seed(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seeded.append(payload)
        return payload


def _app(project: _FakeProject, *, grant: set[str], seen: list[dict[str, Any]], task: _FakeTask | None = None) -> FastAPI:
    """`grant` is the set of relations this subject holds — everything else is denied."""

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        seen.append({"user": user, "relation": relation, "obj": obj})
        return relation in grant

    app = FastAPI()
    register_handlers(app)
    app.include_router(ev.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    return app


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch both proxies once; each test picks the grant set."""
    project, task = _FakeProject(), _FakeTask()
    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    monkeypatch.setattr(ev, "_task_proxy", lambda _t: task)

    def build(grant: set[str]) -> tuple[TestClient, _FakeProject, _FakeTask, list[dict[str, Any]]]:
        seen: list[dict[str, Any]] = []
        return TestClient(_app(project, grant=grant, seen=seen, task=task)), project, task, seen

    return build


ALL_PUBLISH_DOORS = {"can_publish", "can_create_table", "can_promote"}


# --------------------------------------------------------------------------------------------------
# The publish doors — §6.2
# --------------------------------------------------------------------------------------------------


def test_publish_needs_every_door(wired: Any) -> None:
    client, project, _t, seen = wired(ALL_PUBLISH_DOORS)

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"})

    assert r.status_code == 200, r.text
    relations = [c["relation"] for c in seen]
    assert relations == ["can_publish", "can_create_table", "can_promote"], "the doors must be crossed in order"
    assert seen[0]["obj"] == "annotation_project:p1", "door 1 is the annotator's own domain"
    assert seen[1]["obj"] == "namespace:silver", "door 2 is the TARGET NAMESPACE, not the project"
    assert project.fired, "the transition never reached the actor"


@pytest.mark.parametrize(
    ("held", "closed"),
    [
        ({"can_create_table", "can_promote"}, "can_publish"),
        ({"can_publish", "can_promote"}, "can_create_table"),
        ({"can_publish", "can_create_table"}, "can_promote"),
    ],
)
def test_holding_only_some_doors_is_403(wired: Any, held: set[str], closed: str) -> None:
    """Half the authority is not authority. Each door is load-bearing on its own."""
    client, project, _t, _seen = wired(held)

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"})

    assert r.status_code == 403
    assert closed in r.text, "the refusal must name the door that closed"
    assert project.fired == [], "the publish reached the actor despite a closed door"


def test_the_audit_names_the_first_door_that_closed_not_a_composite(wired: Any) -> None:
    """Short-circuiting is deliberate: "publish denied" is not actionable, "lacks can_create_table on
    namespace:silver" is."""
    client, _p, _t, seen = wired(set())

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"})

    assert r.status_code == 403
    assert len(seen) == 1, "later doors were checked after the first one closed"
    assert "can_publish" in r.text


def test_an_ungated_namespace_skips_the_promote_door(wired: Any) -> None:
    """Door 3 is conditional — only a validator-gated medallion stage. A publish into a plain
    namespace must not demand a rung that namespace does not have."""
    client, _p, _t, seen = wired({"can_publish", "can_create_table"})

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme", "target_namespace": "scratch"})

    assert r.status_code == 200, r.text
    assert [c["relation"] for c in seen] == ["can_publish", "can_create_table"]
    assert seen[1]["obj"] == "namespace:scratch", "the doors are checked wherever the publish points"


def test_the_default_target_is_silver(wired: Any) -> None:
    """Human labels are curated, not raw (§6.2)."""
    client, _p, _t, seen = wired(ALL_PUBLISH_DOORS)
    client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"})
    assert seen[1]["obj"] == "namespace:silver"


def test_an_actor_precondition_failure_is_409_not_500(wired: Any) -> None:
    """Every-task-terminal lives in the ACTOR, so it holds for any caller — including a workflow
    retrying after a crash. The endpoint must surface it as a conflict, not an error."""
    client, project, _t, _seen = wired(ALL_PUBLISH_DOORS)
    project.raise_on_fire = IllegalTransition("project", "frozen", "publish (tasks are not all terminal)")

    r = client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"})

    assert r.status_code == 409
    assert "not all terminal" in r.text


# --------------------------------------------------------------------------------------------------
# The other edges read their permission from the machine
# --------------------------------------------------------------------------------------------------


def test_a_non_publish_edge_checks_the_permission_the_table_declares(wired: Any) -> None:
    client, _p, _t, seen = wired({"can_manage"})
    r = client.post("/projects/p1/events", json={"event": "open", "tenant": "acme"})
    assert r.status_code == 200, r.text
    assert seen == [{"user": SUBJECT, "relation": "can_manage", "obj": "project:acme"}]


def test_an_edge_absent_from_the_table_is_409(wired: Any) -> None:
    client, _p, _t, _seen = wired({"can_manage"})
    r = client.post("/projects/p1/events", json={"event": "publish_succeeded", "tenant": "acme"})
    assert r.status_code == 409


def test_an_absent_project_is_409_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    project = _FakeProject(state=None)
    monkeypatch.setattr(ev, "_project_proxy", lambda _p: project)
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(project, grant=ALL_PUBLISH_DOORS, seen=seen))
    assert client.post("/projects/p1/events", json={"event": "publish", "tenant": "acme"}).status_code == 409


# --------------------------------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------------------------------


def _item(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "project_id": "ignored",
        "source": {"kind": "chunks", "keys": [task_id]},
        "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},
    }


def test_send_seeds_the_task_actor_before_indexing_it(wired: Any) -> None:
    """The order is the correctness argument. A crash between the two leaves a task that exists but
    is not indexed — invisible to the publish precondition, and repaired by an idempotent re-send.
    The reverse would index a task whose actor was never seeded, so the precondition would read a
    state for a task that cannot answer for itself."""
    client, project, task, _seen = wired({"can_send_items"})

    r = client.post("/projects/p1/items", json={"tenant": "acme", "items": [_item("t0")]})

    assert r.status_code == 201, r.text
    assert task.seeded, "the task actor was never seeded"
    assert project.sent, "the task was never indexed"
    assert task.seeded[0]["task_id"] == project.sent[0]["task_id"]


def test_send_forces_the_project_id_from_the_path(wired: Any) -> None:
    """A client-supplied `project_id` in the body must not be able to file a task under a project the
    caller was not authorized against."""
    client, _p, task, _seen = wired({"can_send_items"})
    client.post("/projects/p1/items", json={"tenant": "acme", "items": [_item("t0")]})
    assert task.seeded[0]["project_id"] == "p1"


def test_send_without_the_permission_writes_nothing(wired: Any) -> None:
    client, project, task, _seen = wired(set())
    r = client.post("/projects/p1/items", json={"tenant": "acme", "items": [_item("t0")]})
    assert r.status_code == 403
    assert task.seeded == [] and project.sent == []


def test_listing_tasks_returns_the_precondition_from_the_same_snapshot(wired: Any) -> None:
    client, _p, _t, _seen = wired({"can_view"})
    body = client.get("/projects/p1/tasks", params={"tenant": "acme"}).json()
    assert "may_publish" in body and "counts" in body, "the caller must not have to ask twice"
