"""The four HTTP contracts: the catalog's shape, validation, a run, and reading a run back.

The app is assembled HERE rather than imported as `flows.app`, for the same reason the ingest suite
does it (`test_ingest_api.py`): the module-level app bakes its settings and its lifespan at import,
and a test that needs the lifespan to have run needs a live Serve origin and a dapr sidecar to decide
against. Assembling the routers over an explicit `app.state` tests the request handling — which is
what these contracts are — and leaves the wiring to `test_the_flows_row_*` in the gateway suite,
where it is checked against the real app's openapi.

`/api` is the mount prefix on purpose: it is what the chart and `scripts/dev-micro.sh` set
`RASK_API_PREFIX` to, so the paths driven here are the paths the gateway's `/api/flows` row forwards.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import health, routes
from flows.config import FlowsSettings
from flows.dependencies import FlowScheduler
from service_kit.exceptions import register_handlers


SERVE = "http://serve.test:8000"

ALTO = '<TextLine><String CONTENT="Anno"/><String CONTENT="1723"/></TextLine>'

#: text → model → alto → inspect, the v0 chain the studio palette can draw.
CHAIN: dict[str, Any] = {
    "graph": {
        "nodes": [
            {"id": "t", "kind": "text"},
            {"id": "m", "kind": "model", "config": {"app": "htrflow"}},
            {"id": "a", "kind": "alto"},
            {"id": "s", "kind": "inspect"},
        ],
        "edges": [
            {"source": "t", "target": "m"},
            {"source": "m", "target": "a"},
            {"source": "a", "target": "s"},
        ],
    },
    "seeds": {"t": "page bytes stand-in"},
}


class _RecordingScheduler(FlowScheduler):
    """Records every dispatch, so the durable-lane test can assert that the run was HANDED OVER —
    not merely that the response said `running`. A boundary double, like ingest's `_RecordingStarter`;
    a live sidecar would prove nothing extra about the route's decision."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, object]]] = []

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        self.dispatched.append((run_id, dict(payload)))


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    register_handlers(app)  # so a NotFoundError lands as 404 problem+json, as it does in the real app
    app.include_router(health.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.state.flows_settings = FlowsSettings(serve_url=SERVE, serve_timeout=5.0, max_runs=3)
    app.state.http = httpx.AsyncClient()
    app.state.runs = {}
    app.state.workflow_scheduler = None
    with TestClient(app) as test_client:
        yield test_client


def _runs(client: TestClient) -> dict[str, Any]:
    return client.app.state.runs  # type: ignore[attr-defined]  — the ASGI app is a FastAPI here


# ---- catalog ---------------------------------------------------------------------------------


def test_the_catalog_endpoint_serves_the_pinned_shape(client: TestClient) -> None:
    resp = client.get("/api/flows/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"version", "kinds"}
    assert body["version"] == 1
    assert {k["kind"] for k in body["kinds"]} == {
        "image",
        "text",
        "dataset",
        "prompt",
        "model",
        "mcp",
        "alto",
        "extract",
        "regex",
        "compare",
        "inspect",
    }
    assert set(body["kinds"][0]) == {"kind", "label", "role", "params"}


# ---- validate --------------------------------------------------------------------------------


def test_validate_accepts_the_v0_chain(client: TestClient) -> None:
    resp = client.post("/api/flows/validate", json=CHAIN["graph"])
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "problems": []}


def test_validate_refuses_a_cycle_and_says_why(client: TestClient) -> None:
    graph = {
        "nodes": [{"id": "a", "kind": "text"}, {"id": "b", "kind": "inspect"}],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    }
    body = client.post("/api/flows/validate", json=graph).json()
    assert body["ok"] is False
    assert body["problems"] == ["graph has a cycle"]


# ---- runs ------------------------------------------------------------------------------------


@respx.mock
def test_a_run_executes_inline_and_is_readable_afterwards(client: TestClient) -> None:
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    resp = client.post("/api/flows/runs", json=CHAIN)
    assert resp.status_code == 200
    state = resp.json()
    assert state["status"] == "succeeded"
    assert state["run_id"].startswith("run-")
    assert state["nodes"]["a"]["output_text"] == "Anno 1723"

    # The run resource is the same document, not a re-execution: reading it must not call Serve again.
    again = client.get(f"/api/flows/runs/{state['run_id']}")
    assert again.status_code == 200
    assert again.json() == state
    assert respx.calls.call_count == 1


@respx.mock
def test_a_failing_model_node_blocks_its_dependents(client: TestClient) -> None:
    """A 502 from Serve fails the model node; `alto` and `inspect` are recorded blocked and never
    dispatched. The run is a 200 — the RUN succeeded in reporting what happened, and a sandbox that
    5xx'd on a red node would leave the builder nothing to paint."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(502))

    resp = client.post("/api/flows/runs", json=CHAIN)
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert resp.json()["status"] == "failed"
    assert nodes["t"]["status"] == "succeeded"
    assert nodes["m"]["error"] == "serve returned 502 for app 'htrflow'"
    assert nodes["a"]["error"] == "upstream failed"
    assert nodes["s"]["error"] == "upstream failed"
    assert nodes["a"]["ms"] is None


def test_a_graph_that_does_not_validate_is_refused_with_the_problem_LIST(client: TestClient) -> None:
    """422 + problem+json + the structured `problems`. The list is the point: the builder highlights
    the offending nodes, which a single flattened `detail` string cannot support."""
    resp = client.post(
        "/api/flows/runs",
        json={"graph": {"nodes": [{"id": "a", "kind": "quantum"}], "edges": []}, "seeds": {}},
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["problems"] == ["unknown node kind: quantum (node a)"]
    assert body["status"] == 422
    # Refused, therefore never a run: a rejected graph must not leave a resource behind.
    assert _runs(client) == {}


def test_an_unknown_run_id_is_404(client: TestClient) -> None:
    resp = client.get("/api/flows/runs/run-nope")
    assert resp.status_code == 404
    assert "run-nope" in resp.json()["detail"]


def test_the_durable_lane_takes_over_when_a_scheduler_is_present(client: TestClient) -> None:
    """With a sidecar the route SCHEDULES instead of executing: `running`, no node states, and the
    engine owns the run from there. The lane is decided at startup, never by the caller."""
    scheduler = _RecordingScheduler()
    client.app.state.workflow_scheduler = scheduler  # type: ignore[attr-defined]

    state = client.post("/api/flows/runs", json=CHAIN).json()
    assert state["status"] == "running"
    assert state["nodes"] == {}

    assert len(scheduler.dispatched) == 1
    run_id, payload = scheduler.dispatched[0]
    assert run_id == state["run_id"]
    # The Serve origin is RESOLVED at schedule time and carried: an activity may execute on any
    # worker after any replay, and re-reading it there is how one run talks to two clusters.
    assert payload["serve_url"] == SERVE
    assert payload["seeds"] == {"t": "page bytes stand-in"}
    # Readable immediately, so a client can poll the same resource in either lane.
    assert client.get(f"/api/flows/runs/{run_id}").status_code == 200


@respx.mock
def test_the_run_store_is_bounded_and_evicts_the_oldest(client: TestClient) -> None:
    """`max_runs` is a memory bound on a process-local dict, not a retention policy — stated because
    an unbounded dict on a long-lived pod is a leak that only shows up in production."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    ids = [client.post("/api/flows/runs", json=CHAIN).json()["run_id"] for _ in range(4)]

    assert len(_runs(client)) == 3  # max_runs from the fixture
    assert client.get(f"/api/flows/runs/{ids[0]}").status_code == 404
    assert client.get(f"/api/flows/runs/{ids[-1]}").status_code == 200
