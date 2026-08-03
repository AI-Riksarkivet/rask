"""The projects read surface — what A1's landing and detail pages actually call.

Three contracts pinned here:

1. `GET /projects?tenant=` lists through the TENANT index actor and is gated on tenant membership —
   a member sees the tenant's projects, an outsider sees a 403, never an empty list that reads as
   "no projects".
2. `GET /projects/{id}` is `can_view`-gated and carries `legal_events` derived from the MACHINE
   TABLES — the single source the UI renders so it never hardcodes a second copy that drifts.
3. Create REGISTERS the project in the tenant index, so a project cannot exist and be unlisted.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.projects.models import AnnotationProject, ProjectState
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


def _doc(project_id: str, state: ProjectState = ProjectState.LABELING) -> dict[str, Any]:
    return AnnotationProject(project_id=project_id, tenant="acme", slug=f"slug-{project_id}", state=state).model_dump(mode="json")


class _FakeProjectActor:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc
        self.created: list[dict[str, Any]] = []

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created.append(payload)
        return payload

    async def get(self) -> dict[str, Any] | None:
        return dict(self.doc) if self.doc else None


class _FakeTenantActor:
    def __init__(self, ids: list[str] | None = None) -> None:
        self.ids = list(ids or [])
        self.registered: list[str] = []

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.registered.append(str(payload["project_id"]))
        created = payload["project_id"] not in self.ids
        if created:
            self.ids.append(str(payload["project_id"]))
        return {"project_id": payload["project_id"], "created": created, "total": len(self.ids)}

    async def list_projects(self) -> dict[str, Any]:
        return {"project_ids": list(self.ids), "total": len(self.ids)}


def _app(*, grant: set[str], seen: list[dict[str, Any]] | None = None) -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return relation in grant

    app = FastAPI()
    app.include_router(projects_ep.router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_fga_client] = lambda: None
    return app


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    def build(
        *,
        grant: set[str],
        projects: dict[str, dict[str, Any] | None] | None = None,
        tenant_ids: list[str] | None = None,
    ) -> tuple[TestClient, _FakeTenantActor, list[dict[str, Any]]]:
        docs = projects or {}
        tenant = _FakeTenantActor(tenant_ids)
        monkeypatch.setattr(projects_ep, "_create_actor", lambda pid: _FakeProjectActor(docs.get(pid)))
        monkeypatch.setattr(projects_ep, "_tenant_actor", lambda _t: tenant)
        seen: list[dict[str, Any]] = []
        return TestClient(_app(grant=grant, seen=seen)), tenant, seen

    return build


# --------------------------------------------------------------------------------------------------
# GET /projects — the landing list
# --------------------------------------------------------------------------------------------------


def test_a_member_lists_the_tenants_projects(wired: Any) -> None:
    client, _tenant, seen = wired(
        grant={"member"},
        tenant_ids=["p1", "p2"],
        projects={"p1": _doc("p1"), "p2": _doc("p2", ProjectState.FROZEN)},
    )

    r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["project_id"] for p in body["projects"]] == ["p1", "p2"]
    assert body["projects"][1]["state"] == "frozen"
    assert seen[0] == {"user": "gina", "relation": "member", "obj": "project:acme"}


def test_a_non_member_gets_403_not_an_empty_list(wired: Any) -> None:
    client, _tenant, _seen = wired(grant=set(), tenant_ids=["p1"], projects={"p1": _doc("p1")})
    r = client.get("/projects", params={"tenant": "acme"})
    assert r.status_code == 403, "an outsider must be REFUSED — an empty 200 reads as 'no projects'"


def test_a_registered_but_stateless_project_is_skipped_not_a_500(wired: Any) -> None:
    """The index can briefly lead the project actor (a crash between register and... no — register
    follows create, but a lost actor partition is a real failure mode). A ghost id must not take
    the whole landing down."""
    client, _tenant, _seen = wired(grant={"member"}, tenant_ids=["p1", "ghost"], projects={"p1": _doc("p1"), "ghost": None})
    r = client.get("/projects", params={"tenant": "acme"})
    assert r.status_code == 200
    assert [p["project_id"] for p in r.json()["projects"]] == ["p1"]


# --------------------------------------------------------------------------------------------------
# GET /projects/{id} — the detail read A1 renders
# --------------------------------------------------------------------------------------------------


def test_get_project_carries_legal_events_from_the_machine_tables(wired: Any) -> None:
    client, _tenant, seen = wired(grant={"can_view"}, projects={"p1": _doc("p1", ProjectState.FROZEN)})

    r = client.get("/projects/p1")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"]["state"] == "frozen"
    assert {(e["event"], e["permission"]) for e in body["legal_events"]} == {
        ("open", "can_manage"),
        ("publish", "can_publish"),
        ("archive", "can_manage"),
    }
    assert seen[0] == {"user": "gina", "relation": "can_view", "obj": "annotation_project:p1"}


def test_get_project_without_can_view_is_403(wired: Any) -> None:
    client, _tenant, _seen = wired(grant=set(), projects={"p1": _doc("p1")})
    assert client.get("/projects/p1").status_code == 403


def test_get_missing_project_is_404(wired: Any) -> None:
    client, _tenant, _seen = wired(grant={"can_view"}, projects={})
    assert client.get("/projects/nope").status_code == 404


# --------------------------------------------------------------------------------------------------
# Create registers in the tenant index
# --------------------------------------------------------------------------------------------------


def test_create_registers_the_project_in_the_tenant_index(wired: Any) -> None:
    client, tenant, _seen = wired(grant={"can_create_annotation_project"})

    r = client.post("/projects", json={"tenant": "acme", "slug": "labels"})

    assert r.status_code == 201, r.text
    assert tenant.registered == [r.json()["project_id"]], "an unregistered project is invisible to the landing forever"
