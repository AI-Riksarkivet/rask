"""`GET /api/projects/` answered anonymously, and it enumerates every tenant in the estate.

The response carries each project's slug, team, workload type, k8s namespace and LIVE INGRESS HOST —
estate-wide tenant enumeration, on a route `gateway/__init__.py` publishes at the edge. The catalog
gates the same class of read on `can_observe_events`; this service had no auth code path at all, so
there was nothing to gate with.

These are the request-level half of the contract: the structural gate in
`tests/unit/test_every_public_service_has_a_door.py` proves the service CAN authenticate and that the
chart feeds it; this proves the door refuses, allows, and stays open on a dev stack.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from controlplane import routes, security
from service_kit.exceptions import register_handlers


class _Reader:
    def list_projects(self) -> list[object]:
        return []


def _app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.get_reader] = lambda: _Reader()
    return app


def test_a_denied_subject_gets_a_403_that_NAMES_the_missing_tuple() -> None:
    app = _app()

    async def _deny(*, user: str, relation: str, obj: str) -> bool:
        return False

    app.dependency_overrides[security._deps.current_subject] = lambda: "mallory"
    app.dependency_overrides[security._deps.get_checker] = lambda: _deny
    with TestClient(app) as client:
        resp = client.get("/api/projects/")

    assert resp.status_code == 403, f"the project list answered {resp.status_code} to a denied caller"
    detail = resp.json()["detail"]
    # The estate's FGA-denial format: <subject> lacks <relation> on <object> — the fix is in the message.
    assert "mallory" in detail
    assert security.READ in detail


def test_an_allowed_subject_gets_the_list() -> None:
    app = _app()

    async def _allow(*, user: str, relation: str, obj: str) -> bool:
        return True

    app.dependency_overrides[security._deps.current_subject] = lambda: "alice"
    app.dependency_overrides[security._deps.get_checker] = lambda: _allow
    with TestClient(app) as client:
        resp = client.get("/api/projects/")

    assert resp.status_code == 200
