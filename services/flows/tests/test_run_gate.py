"""The run door's authorization contract (#56).

``POST /flows/runs`` is the one flows route that spends compute, so it is the one that checks the
estate ``writer`` tier on the root object. These pin the four outcomes: defaults stay open (a dev
stack is unchanged), a denial is a 403 that NAMES the missing tuple, an allow proceeds to the run,
and a governed stack whose auth layer failed to build answers 503 — never a silent allow.

The app is assembled like ``test_routes.py`` does (explicit routers over an explicit ``app.state``);
the subject/checker sub-dependencies are overridden at their own seams
(``security._deps.current_subject`` / ``security._deps.get_checker``), which leaves the ROUTE's own
wiring — the check call, the audit, the refusal — exercised for real.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import routes, security
from flows.config import FlowsSettings
from service_kit.exceptions import register_handlers


GRAPH = {
    "nodes": [{"id": "t", "kind": "text"}, {"id": "s", "kind": "inspect"}],
    "edges": [{"source": "t", "target": "s"}],
}
BODY = {"graph": GRAPH, "seeds": {"t": "hello"}}


def _app(settings: FlowsSettings) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.state.flows_settings = settings
    app.state.http = httpx.AsyncClient()
    app.state.runs = {}
    app.state.workflow_scheduler = None
    return app


@pytest.fixture
def open_client() -> Iterator[TestClient]:
    """Auth entirely off — the defaults every existing deployment has."""
    with TestClient(_app(FlowsSettings(serve_url="http://serve.invalid:8000"))) as client:
        yield client


def test_the_defaults_leave_the_door_open(open_client: TestClient) -> None:
    """No bearer, no FGA — a dev stack runs flows exactly as before the gate existed."""
    resp = open_client.post("/api/flows/runs", json=BODY)
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


def test_a_denial_is_a_403_that_names_the_missing_tuple() -> None:
    app = _app(FlowsSettings(serve_url="http://serve.invalid:8000"))

    async def _deny(*, user: str, relation: str, obj: str) -> bool:
        return False

    app.dependency_overrides[security._deps.current_subject] = lambda: "mallory"
    app.dependency_overrides[security._deps.get_checker] = lambda: _deny
    with TestClient(app) as client:
        resp = client.post("/api/flows/runs", json=BODY)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    # The estate's FGA-denial format: <subject> lacks <relation> on <object> — the fix is in the message.
    assert "mallory" in detail
    assert "writer" in detail
    assert "warehouse:lance_catalog" in detail


def test_an_allow_proceeds_to_the_run() -> None:
    app = _app(FlowsSettings(serve_url="http://serve.invalid:8000"))

    async def _allow(*, user: str, relation: str, obj: str) -> bool:
        return True

    app.dependency_overrides[security._deps.current_subject] = lambda: "alice"
    app.dependency_overrides[security._deps.get_checker] = lambda: _allow
    with TestClient(app) as client:
        resp = client.post("/api/flows/runs", json=BODY)
    assert resp.status_code == 200


def test_a_governed_stack_with_no_verifier_is_a_503_not_an_open_door() -> None:
    """OIDC on but the verifier failed to build → the door answers 503, with or without a bearer.

    This is the three-outcome contract's middle case: a broken authorization layer must present as
    an outage, never as an open door.
    """
    settings = FlowsSettings(
        serve_url="http://serve.invalid:8000",
        oidc_enabled=True,
        oidc_issuer="http://dex.test/dex",
        oidc_audience="rask",
        fga_enabled=True,
    )
    with TestClient(_app(settings)) as client:
        resp = client.post("/api/flows/runs", json=BODY, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_the_read_routes_stay_open(open_client: TestClient) -> None:
    """/catalog and /validate read a server-declared registry and run graph hygiene — no compute,
    no gate. Pinned so adding one later is a deliberate act, not a side effect."""
    assert open_client.get("/api/flows/catalog").status_code == 200
    assert open_client.post("/api/flows/validate", json=GRAPH).status_code == 200
