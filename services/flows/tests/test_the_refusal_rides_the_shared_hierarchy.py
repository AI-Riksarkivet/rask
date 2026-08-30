"""`create_run` refuses through the estate's exception hierarchy, not around it.

`create_run` was declared `-> RunState | JSONResponse` and hand-built its own problem+json body,
for one stated reason: `service_kit.exceptions._problem` flattened a problem to four keys, and the
refusal's whole point is a structured `problems` LIST the builder highlights nodes from
(FLOWS-422-BYPASSES-HIERARCHY). So the one route in this service that can refuse for a domain reason
was also the one route whose refusal no handler produced — a second error plane, one route wide.

The cost is not theoretical. A union return type means the next refusal added here can return any
`JSONResponse` at all with nothing checking its shape, and the hand-built body is a private copy of a
document the estate owns: `_problem` gaining a member (or changing one) leaves this route answering
the old shape, silently, forever.

The hierarchy now carries RFC 9457 extension members (`packages/service-kit/tests/
test_problem_extension_members.py`), so the reason is gone and so is the escape hatch. The refusal
keeps its specific `about:blank#flow-invalid` type — a domain naming its own failure is the point of
the taxonomy, not a departure from it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import health, routes
from flows.config import FlowsSettings
from flows.models import RunState
from service_kit.exceptions import register_handlers


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The routers over an explicit `app.state`, the shape `test_routes.py` documents — and with
    `register_handlers` mounted, which is the whole point here: the refusal must be PRODUCED by that
    handler rather than assembled beside it."""
    app = FastAPI()
    register_handlers(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.state.flows_settings = FlowsSettings(serve_url="http://serve.invalid:8000", serve_timeout=5.0, max_runs=3)
    app.state.http = httpx.AsyncClient()
    app.state.runs = {}
    app.state.workflow_scheduler = None
    app.state.workflow_reader = None
    with TestClient(app) as test_client:
        yield test_client


def test_create_run_declares_one_return_type() -> None:
    """The `| JSONResponse` escape hatch is what let a second error plane exist here."""
    from flows.routes import create_run

    assert inspect.signature(create_run).return_annotation is RunState


def test_the_refusal_body_is_the_shared_builders_output(client: TestClient) -> None:
    """Five members, produced by the one handler every other refusal in this service goes through."""
    response = client.post(
        "/api/flows/runs",
        json={"graph": {"nodes": [{"id": "a", "kind": "quantum"}], "edges": []}, "seeds": {}},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "about:blank#flow-invalid",
        "title": "Unprocessable Entity",
        "status": 422,
        "detail": "1 problem(s) in the graph",
        "problems": ["unknown node kind: quantum (node a)"],
    }


def test_the_refusal_is_raised_so_it_can_be_caught(client: TestClient) -> None:
    """A refusal that is RAISED is one the service layer can produce; a returned response is not.

    This is the practical difference the union type hid: `validate_graph` is called from the route
    today, but anything below the route that wants to refuse a graph could not, because the only
    refusal path was `return JSONResponse(...)` from inside a handler.
    """
    from flows.models import RunRefusedError
    from service_kit.exceptions import UnprocessableEntityError

    error = RunRefusedError("1 problem(s) in the graph", extensions={"problems": ["x"]})
    assert isinstance(error, UnprocessableEntityError)
    assert error.status_code == 422
