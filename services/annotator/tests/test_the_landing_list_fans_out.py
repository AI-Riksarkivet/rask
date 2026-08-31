"""ANN-03 — the landing list reads every project's actor concurrently (the hottest of the three sites).

`GET /projects?tenant=` fans out to one actor per registered project — DIFFERENT actor ids, so the
reads genuinely parallelise rather than queueing on one actor's turn lock (the distinction that made
the send path need `SendMany` instead of a fan-out). Awaited in a bare `for` loop, the landing's wall
clock is one sidecar round-trip per project, on the read every page load makes.

The BOUND is half the contract: an unbounded gather over a tenant with a thousand projects opens a
thousand simultaneous sidecar channels, which trades a slow landing for a broken sidecar.

Three properties the fan-out must not change, all pinned below — the rows come back in the tenant
index's order, a project whose actor holds no state is still SKIPPED rather than taking the landing
down, and a project whose actor read FAILS still fails the whole listing rather than serving one that
silently omits it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import current_subject, get_checker, get_fga_client
from annotator.api.v1.endpoints import projects as projects_ep
from annotator.projects.models import AnnotationProject
from service_kit.exceptions import register_handlers


class _Gauge:
    """High-water mark of simultaneous in-flight actor reads.

    No lock: every increment happens on the one event loop the app runs on, and the `sleep` is the
    only suspension point — so a sequential loop can never register a peak above 1.
    """

    def __init__(self) -> None:
        self._live = 0
        self.peak = 0

    async def hold(self) -> None:
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            await asyncio.sleep(0.01)
        finally:
            self._live -= 1


class _FakeProjectActor:
    def __init__(self, doc: dict[str, Any] | None, gauge: _Gauge, *, boom: bool = False) -> None:
        self._doc = doc
        self._gauge = gauge
        self._boom = boom

    async def get(self) -> dict[str, Any] | None:
        await self._gauge.hold()
        if self._boom:
            raise RuntimeError("the actor's state store is unreachable")
        return dict(self._doc) if self._doc else None


class _FakeTenantActor:
    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)

    async def list_projects(self) -> dict[str, Any]:
        return {"project_ids": list(self._ids), "total": len(self._ids)}


def _doc(project_id: str) -> dict[str, Any]:
    return AnnotationProject(project_id=project_id, tenant="acme", slug=f"slug-{project_id}").model_dump(mode="json")


def _client(
    monkeypatch: pytest.MonkeyPatch, *, ids: list[str], gauge: _Gauge, missing: frozenset[str] = frozenset(), broken: frozenset[str] = frozenset()
) -> TestClient:
    def create_actor(project_id: str) -> Any:
        doc = None if project_id in missing else _doc(project_id)
        return _FakeProjectActor(doc, gauge, boom=project_id in broken)

    monkeypatch.setattr(projects_ep, "_create_actor", create_actor)
    monkeypatch.setattr(projects_ep, "_tenant_actor", lambda _t: _FakeTenantActor(ids))

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    app = FastAPI()
    app.include_router(projects_ep.router)
    register_handlers(app)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: "gina"
    app.dependency_overrides[get_fga_client] = lambda: None
    # The listing's failure answer is the assertion, not the traceback — `raise_server_exceptions`
    # would hand back the exception instead of the response the caller actually receives.
    return TestClient(app, raise_server_exceptions=False)


def test_the_landing_reads_its_projects_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    gauge = _Gauge()
    ids = [f"p{n}" for n in range(8)]

    client = _client(monkeypatch, ids=ids, gauge=gauge)
    r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 200, r.text
    assert gauge.peak > 1, "the project actors are still read one after another — the landing costs one round-trip per project"
    assert [p["project_id"] for p in r.json()["projects"]] == ids, "the tenant index's order must survive the fan-out"


def test_the_fan_out_is_bounded_rather_than_one_channel_per_project(monkeypatch: pytest.MonkeyPatch) -> None:
    gauge = _Gauge()
    ids = [f"p{n:03d}" for n in range(64)]

    client = _client(monkeypatch, ids=ids, gauge=gauge)
    r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 200, r.text
    assert r.json()["total"] == 64
    assert gauge.peak <= projects_ep._LISTING_FANOUT, f"{gauge.peak} simultaneous actor reads — the fan-out is unbounded"


def test_a_stateless_project_is_still_skipped_rather_than_taking_the_landing_down(monkeypatch: pytest.MonkeyPatch) -> None:
    gauge = _Gauge()

    client = _client(monkeypatch, ids=["p1", "ghost", "p2"], gauge=gauge, missing=frozenset({"ghost"}))
    r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["project_id"] for p in body["projects"]] == ["p1", "p2"], "a lost partition drops its row and the neighbours keep their order"
    assert body["total"] == 2, "`total` counts the rows returned, not the ids the index held"


def test_a_failed_actor_read_still_fails_the_whole_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable actor must be an error, not a shorter page: a listing that silently omits a
    project the caller may see is indistinguishable from one that was deleted."""
    gauge = _Gauge()

    client = _client(monkeypatch, ids=["p1", "p2", "p3"], gauge=gauge, broken=frozenset({"p2"}))
    r = client.get("/projects", params={"tenant": "acme"})

    assert r.status_code == 500, f"a failed actor read was swallowed into a partial listing: {r.text}"
