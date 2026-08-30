"""ANN-10 — `POST /projects` performs three writes in a row and, until now, protected none of them.

The sequence is: seed the project actor, register the id in the tenant index, seed the FGA owner
tuples. A failure after the first leaves a project that EXISTS and cannot be used, in one of two
shapes:

* register failed → the document is in the state store, absent from the tenant index, and holds no
  tuples. Nothing lists it and nobody can open it. It is unreachable, permanently.
* the grant failed → it is listed on the landing page and every `can_view` / `can_manage` check
  against it denies, for everyone, creator included. Worse than invisible: visible and dead.

The endpoint's own docstring said a failure "is answered by retrying the create, not by a repair
job", and that was false in the way that matters: `AnnotationProject.project_id` is a
`default_factory=new_id`, so a retry mints a NEW id and creates a SECOND project. The first one is
not repaired by the retry — it is orphaned by it.

So the create compensates: it undoes what it wrote, in reverse, and then fails. And the
compensation is narrow by construction — `discard` refuses any project that is not an empty draft,
so it can never become a delete-project door.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.projects.machines import IllegalTransition
from annotator.projects.models import AnnotationProject, ProjectState, Task, TaskState
from annotator.projects.project_actor import DROPPED_KEY, INDEX_KEY, PROJECT_KEY, AnnotationProjectActor
from annotator.projects.tenant_actor import PROJECTS_KEY, TenantProjectsActor
from service_kit.exceptions import register_handlers


# --------------------------------------------------------------------------------------------------
# The endpoint: what is left behind when a write in the middle of the sequence fails
# --------------------------------------------------------------------------------------------------


class _FakeProjectActor:
    def __init__(self) -> None:
        self.doc: dict[str, Any] | None = None
        self.discards = 0

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.doc = payload
        return payload

    async def get(self) -> dict[str, Any] | None:
        return self.doc

    async def discard(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self.discards += 1
        discarded = self.doc is not None
        self.doc = None
        return {"discarded": discarded}


class _FakeTenantActor:
    def __init__(self, *, register_fails: bool = False) -> None:
        self.ids: list[str] = []
        self.register_fails = register_fails

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.register_fails:
            raise RuntimeError("the tenant index actor is unreachable")
        self.ids.append(str(payload["project_id"]))
        return {"project_id": payload["project_id"], "created": True, "total": len(self.ids)}

    async def unregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        removed = str(payload["project_id"]) in self.ids
        self.ids = [i for i in self.ids if i != str(payload["project_id"])]
        return {"project_id": payload["project_id"], "removed": removed, "total": len(self.ids)}


def _wire(monkeypatch: pytest.MonkeyPatch, project: _FakeProjectActor, tenant: _FakeTenantActor, *, grant_fails: bool) -> TestClient:
    monkeypatch.setattr(projects_ep, "_create_actor", lambda _pid: project)
    monkeypatch.setattr(projects_ep, "_tenant_actor", lambda _t: tenant)

    async def grant_on_create(*_args: Any, **_kwargs: Any) -> None:
        if grant_fails:
            raise RuntimeError("OpenFGA refused the write")

    monkeypatch.setattr(projects_ep.fga, "grant_on_create", grant_on_create)

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    app = FastAPI()
    app.include_router(projects_ep.router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_fga_client] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _create(client: TestClient) -> Any:
    return client.post("/projects", json={"tenant": "acme", "slug": "charters"})


def test_a_failed_index_registration_leaves_no_project_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    project, tenant = _FakeProjectActor(), _FakeTenantActor(register_fails=True)
    client = _wire(monkeypatch, project, tenant, grant_fails=False)

    assert _create(client).status_code >= 500
    assert project.doc is None, "the project document survived a create that failed — unlisted, ungranted and unreachable forever"
    assert project.discards == 1


def test_a_failed_ownership_grant_unregisters_and_discards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worse of the two: listed on the landing page and denying every check, for everyone."""
    project, tenant = _FakeProjectActor(), _FakeTenantActor()
    client = _wire(monkeypatch, project, tenant, grant_fails=True)

    assert _create(client).status_code >= 500
    assert tenant.ids == [], "the project stayed in the tenant index with no owner tuples — visible and permanently 403"
    assert project.doc is None


def test_a_create_that_succeeds_compensates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode that would hide the fix: compensating unconditionally also passes above."""
    project, tenant = _FakeProjectActor(), _FakeTenantActor()
    client = _wire(monkeypatch, project, tenant, grant_fails=False)

    response = _create(client)
    assert response.status_code == 201, response.text
    assert project.doc is not None
    assert project.discards == 0
    assert tenant.ids == [response.json()["project_id"]]


# --------------------------------------------------------------------------------------------------
# The two compensating actor methods
# --------------------------------------------------------------------------------------------------


class _FakeStateManager:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def try_remove_state(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    async def save_state(self) -> None:
        return None


class _ProjectActor(AnnotationProjectActor):
    def __init__(self) -> None:  # noqa: D107 - bypasses Actor.__init__, which needs a runtime
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)


class _TenantActor(TenantProjectsActor):
    def __init__(self) -> None:  # noqa: D107 - as above
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)


def _seeded_project(**kw: Any) -> dict[str, Any]:
    return AnnotationProject(tenant="acme", slug="charters", **kw).model_dump(mode="json")


@pytest.mark.asyncio
async def test_discard_erases_a_project_that_never_finished_being_created() -> None:
    actor = _ProjectActor()
    await actor.create(_seeded_project())

    assert (await actor.discard({}))["discarded"] is True
    assert await actor.get() is None
    assert actor.sm.store == {}, f"state survived the discard: {sorted(actor.sm.store)}"


@pytest.mark.asyncio
async def test_discarding_an_absent_project_is_a_no_op_not_a_failure() -> None:
    """The compensation runs on a path that has already failed once; it must not fail again."""
    assert (await _ProjectActor().discard({}))["discarded"] is False


@pytest.mark.asyncio
async def test_discard_refuses_a_project_that_is_not_an_empty_draft() -> None:
    """The narrowness IS the safety: `discard` must never become a delete-project door."""
    actor = _ProjectActor()
    await actor.create(_seeded_project())
    task = Task(task_id="t1", project_id="p1", source={"kind": "chunks", "keys": ["t1"]}, media={"kind": "image", "image_url": "s3://b/t1.jpg"})
    actor.sm.store[INDEX_KEY] = json.dumps({task.task_id: TaskState.UNASSIGNED.value})

    with pytest.raises(IllegalTransition, match="discard"):
        await actor.discard({})
    assert PROJECT_KEY in actor.sm.store


@pytest.mark.asyncio
async def test_discard_refuses_a_project_that_has_left_draft() -> None:
    actor = _ProjectActor()
    await actor.create(_seeded_project(state=ProjectState.LABELING))

    with pytest.raises(IllegalTransition, match="discard"):
        await actor.discard({})


@pytest.mark.asyncio
async def test_unregister_removes_the_id_and_is_idempotent() -> None:
    actor = _TenantActor()
    await actor.register({"project_id": "p1"})
    await actor.register({"project_id": "p2"})

    assert (await actor.unregister({"project_id": "p1"}))["removed"] is True
    assert (await actor.unregister({"project_id": "p1"}))["removed"] is False
    assert json.loads(actor.sm.store[PROJECTS_KEY]) == ["p2"]


@pytest.mark.asyncio
async def test_the_dropped_key_is_erased_too_so_nothing_of_the_project_remains() -> None:
    actor = _ProjectActor()
    await actor.create(_seeded_project())
    actor.sm.store[DROPPED_KEY] = json.dumps([])

    await actor.discard({})

    assert actor.sm.store == {}
