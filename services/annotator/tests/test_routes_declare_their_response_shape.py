"""ANN-07 — half the routes returned `dict[str, Any]`, so raw actor documents reached clients.

Thirteen of this service's handlers were annotated `-> dict[str, Any]` and none declared a
`response_model`. Two consequences, and the second is the one that bites:

* `/openapi.json` described those responses as "an object", so the generated schema said nothing —
  the frontend's valibot schemas are the ONLY statement of these shapes, on the far side of the
  wire, maintained by hand.
* whatever the actor document happens to hold is what ships. A field added to `Task` for the
  actor's own bookkeeping is published to every client the moment it is stored, and a document that
  has drifted from the model is forwarded unexamined rather than refused.

The response models are the fix, and they are the DOMAIN models where a domain model already
describes the payload — `Task`, `Draft`, `AnnotationProject` — so the declaration cannot drift from
what the actor stores. The tests below pin both halves: the schema is declared, and it publishes
exactly what it published before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import project_events as events_ep
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import AnnotationProject, Draft, ProjectState, Shape, Task, TaskState, Transition
from service_kit.exceptions import register_handlers


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _full_task() -> dict[str, Any]:
    """A task with EVERY optional field populated — an empty document proves nothing about filtering."""
    return Task(
        task_id="t1",
        project_id="p1",
        state=TaskState.CLAIMED,
        assignee="dana",
        lease_expires_at=NOW,
        source=cast(Any, {"kind": "chunks", "keys": ["t1"]}),
        media=cast(Any, {"kind": "image", "image_url": "s3://b/t1.jpg"}),
        submitted_by="dana",
        submitted_at=NOW,
        reviewed_by="rae",
        reviewed_at=NOW,
        review_action="accepted",
        replica_of="g1",
        skipped_reason="unreadable",
        lead_time_seconds=12.5,
        prediction=[Shape(shape_id="s1", shape_type=cast(Any, "bbox"), label="letter", source="model")],
        transitions=[Transition(at=NOW, by="dana", event="claim", from_state=TaskState.UNASSIGNED, to_state=TaskState.CLAIMED)],
    ).model_dump(mode="json")


def _full_draft() -> dict[str, Any]:
    return Draft(
        task_id="t1",
        project_id="p1",
        author="dana",
        shapes=[Shape(shape_id="s1", shape_type=cast(Any, "bbox"), label="letter", source="human")],
        revision=4,
        updated_at=NOW,
        origin="human",
    ).model_dump(mode="json")


def _full_project() -> dict[str, Any]:
    return AnnotationProject(
        project_id="p1",
        tenant="acme",
        slug="charters",
        title="Charters",
        description="why",
        instructions="how",
        state=ProjectState.FROZEN,
        created_at=NOW,
        created_by="gina",
        updated_at=NOW,
        publish_error="the catalog said no",
        pending_publish_id="0123456789abcdef",
        pending_publish_at=NOW,
        pending_target_namespace="silver",
        pending_publish_by="gina",
        publish_progress="collecting",
    ).model_dump(mode="json")


class _TaskActor:
    def __init__(self, task: dict[str, Any], draft: dict[str, Any]) -> None:
        self.task, self.draft = task, draft

    async def get(self) -> dict[str, Any]:
        return dict(self.task)

    async def get_draft(self) -> dict[str, Any]:
        return dict(self.draft)


class _ProjectActor:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc

    async def get(self) -> dict[str, Any]:
        return dict(self.doc)


def _client(monkeypatch: pytest.MonkeyPatch, router: Any) -> TestClient:
    task, draft, project = _full_task(), _full_draft(), _full_project()
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _tid: _TaskActor(task, draft))
    monkeypatch.setattr(events_ep, "_project_proxy", lambda _pid: _ProjectActor(project))

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    api = FastAPI()
    api.include_router(router)
    register_handlers(api)
    api.dependency_overrides[get_checker] = lambda: checker
    api.dependency_overrides[current_subject] = lambda: "gina"
    api.dependency_overrides[tasks_ep.require_actor_plane] = lambda: None
    return TestClient(api)


def test_a_draft_read_publishes_the_draft_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, tasks_ep.router)

    body = client.get("/tasks/t1/draft")

    assert body.status_code == 200, body.text
    assert body.json() == _full_draft()


def test_an_actor_document_that_has_drifted_does_not_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding itself: whatever the document holds is what shipped."""
    drifted = _full_task() | {"internal_scratch": {"sidecar": "bookkeeping"}, "operator_note": "do not ship"}
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _tid: _TaskActor(drifted, _full_draft()))

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    api = FastAPI()
    api.include_router(tasks_ep.router)
    register_handlers(api)
    api.dependency_overrides[get_checker] = lambda: checker
    api.dependency_overrides[current_subject] = lambda: "gina"
    api.dependency_overrides[tasks_ep.require_actor_plane] = lambda: None

    body = TestClient(api).get("/tasks/t1").json()

    assert "internal_scratch" not in body
    assert "operator_note" not in body
    assert body == _full_task()


def test_the_plain_task_listing_still_omits_the_details_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """`details`/`missing` are `v.optional(...)` on the far side of the wire — valibot's optional
    accepts UNDEFINED, not null. Publishing them as `null` on the plain listing (which a declared
    response model does by default) would fail the zone's boundary parse for every queue read."""
    listing = {"tasks": {"t1": "claimed"}, "counts": {"claimed": 1}, "total": 1, "terminal": 0, "may_publish": False}

    class _Project:
        async def list_tasks(self) -> dict[str, Any]:
            return dict(listing)

    monkeypatch.setattr(events_ep, "_project_proxy", lambda _pid: _Project())

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    api = FastAPI()
    api.include_router(events_ep.router)
    register_handlers(api)
    api.dependency_overrides[get_checker] = lambda: checker
    api.dependency_overrides[current_subject] = lambda: "gina"

    body = TestClient(api).get("/projects/p1/tasks").json()

    assert body == listing, f"the plain listing gained or lost a key: {sorted(body)}"
