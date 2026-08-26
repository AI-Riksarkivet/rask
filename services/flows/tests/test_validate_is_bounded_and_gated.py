"""`POST /flows/validate` parsed an arbitrary-size graph on the event loop, ungated.

MEASURED BY THE AUDIT AND RE-MEASURED ON THIS CHECKOUT: 500,000 nodes = 23.26 MiB of JSON =
**3.00 s of event-loop block**, and then the handler answers "graph has 500000 nodes, over the
256-node ceiling". The ceiling is real — `graph.py:41` — but it is checked AFTER pydantic has
constructed every one of the nodes, so it bounds the ANSWER and not the WORK. `FlowGraph.nodes`
carried no `max_length`, so the model had nothing to refuse with.

Flows runs one replica and one loop. Three seconds of block there stalls every other flows request
plus `/livez`, so a caller who never gets past validation can still take the service out.

THE MODULE DOCSTRING SAID THIS ROUTE WAS DELIBERATELY OPEN — "the catalog and validate routes stay
open: they read a server-declared registry and run graph hygiene, no cluster involved". That
reasoning is about the EXECUTE tier and it is sound on its own terms; it does not address unmetered
parsing, which is the actual defect. Both halves are fixed here, and the docstring is corrected
rather than left contradicting the code.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import routes, security
from flows.config import FlowsSettings
from flows.models import MAX_GRAPH_NODES
from service_kit.exceptions import register_handlers


def _app(settings: FlowsSettings) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.state.flows_settings = settings
    app.state.http = httpx.AsyncClient()
    app.state.runs = {}
    app.state.workflow_scheduler = None
    app.state.workflow_reader = None
    return app


def _settings() -> FlowsSettings:
    return FlowsSettings(serve_url="http://serve.invalid:8000")


@pytest.fixture
def open_client() -> Iterator[TestClient]:
    """Auth entirely off — the defaults every existing deployment has."""
    with TestClient(_app(_settings())) as client:
        yield client


def _graph(nodes: int) -> dict[str, object]:
    return {"nodes": [{"id": f"n{i}", "kind": "text"} for i in range(nodes)], "edges": []}


def test_a_graph_over_the_ceiling_is_REFUSED_AT_THE_BOUNDARY(open_client: TestClient) -> None:
    """422 from the model, not 200 carrying a problem string.

    The distinction is the whole finding. A 200-with-problems means the server BUILT every node to
    tell you there were too many; a 422 means pydantic stopped at the declared bound. Only the second
    one bounds the work.
    """
    resp = open_client.post("/api/flows/validate", json=_graph(MAX_GRAPH_NODES + 1))

    assert resp.status_code == 422, (
        f"an over-ceiling graph answered {resp.status_code} — it was parsed in full and judged afterwards, which is the unmetered parse this test exists for"
    )


def test_a_graph_AT_the_ceiling_still_validates(open_client: TestClient) -> None:
    """The bound must be the documented one, not a stricter one that breaks real graphs."""
    resp = open_client.post("/api/flows/validate", json=_graph(MAX_GRAPH_NODES))
    assert resp.status_code == 200


def test_the_defaults_leave_validate_open(open_client: TestClient) -> None:
    """Auth off — a dev stack validates exactly as before the gate existed."""
    resp = open_client.post("/api/flows/validate", json=_graph(2))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_validate_is_GATED_on_a_governed_stack() -> None:
    """Owner ruling 2026-08-26: the estate is authenticated, so no route is ungated.

    `create_run` in this same file already carries `CurrentSubject` + `CheckerDep`; `validate` did
    not, which made the run gate's neighbour the way in.
    """
    app = _app(_settings())

    async def _deny(*, user: str, relation: str, obj: str) -> bool:
        return False

    app.dependency_overrides[security._deps.current_subject] = lambda: "mallory"
    app.dependency_overrides[security._deps.get_checker] = lambda: _deny
    with TestClient(app) as client:
        resp = client.post("/api/flows/validate", json=_graph(2))

    assert resp.status_code == 403, f"validate answered {resp.status_code} to a denied subject"
    assert "mallory" in resp.json()["detail"]


def test_the_catalog_is_GATED_on_a_governed_stack() -> None:
    """The palette registry is served to a caller the estate has refused, otherwise."""
    app = _app(_settings())

    async def _deny(*, user: str, relation: str, obj: str) -> bool:
        return False

    app.dependency_overrides[security._deps.current_subject] = lambda: "mallory"
    app.dependency_overrides[security._deps.get_checker] = lambda: _deny
    with TestClient(app) as client:
        resp = client.get("/api/flows/catalog")

    assert resp.status_code == 403, f"the catalog answered {resp.status_code} to a denied subject"
