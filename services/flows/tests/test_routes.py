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
from http import HTTPStatus
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import health, routes
from flows.config import FlowsSettings
from flows.dependencies import FlowScheduler, ScheduleUnconfirmed
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


class _RefusingScheduler(FlowScheduler):
    """Answers with one of the seam's failure outcomes. Which one it is decides whether the route
    may degrade to the inline lane, so the two are driven separately — see `DaprFlowScheduler`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        raise self.error


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
    app.state.workflow_reader = None
    with TestClient(app) as test_client:
        yield test_client


def _runs(client: TestClient) -> dict[str, Any]:
    return client.app.state.runs  # type: ignore[attr-defined]  — the ASGI app is a FastAPI here


# ---- catalog ---------------------------------------------------------------------------------


_LANE_CALLS: list[tuple[int, dict[str, str]]] = []


def _lane_counts() -> dict[str, int]:
    """`flows.runs` increments seen this test, keyed by lane."""
    out: dict[str, int] = {}
    for amount, attrs in _LANE_CALLS:
        lane = attrs.get("lance.flows.lane")
        if lane is not None and amount:
            out[lane] = out.get(lane, 0) + amount
    return out


@pytest.fixture(autouse=True)
def _capture_lane_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counter-double for `flows.runs`, per the house pattern — MeterProvider is process-global and
    set-once, so an in-memory provider would need a rebinding hook in every metrics module."""
    from flows import metrics as flows_metrics

    _LANE_CALLS.clear()
    monkeypatch.setattr(flows_metrics, "_runs", type("C", (), {"add": lambda _s, n, a=None: _LANE_CALLS.append((n, dict(a or {})))})())


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

    assert _lane_counts() == {"inline": 1}, (
        "an inline run records no lane. The lane is decided PER REQUEST and announced only once per "
        f"process at startup, so 'did THIS run execute durably or inline?' is unanswerable — saw {_lane_counts()}. "
        "Latency inverts between the lanes, so mixing them in one series is worse than not having it."
    )


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

    assert _lane_counts() == {"durable": 1}, f"a durable run records no lane: {_lane_counts()}"

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
def test_an_unconfirmed_schedule_refuses_instead_of_running_the_graph_twice(client: TestClient, respx_allows_unused_routes) -> None:
    """`ScheduleUnconfirmed` means the engine may already be running this graph, so the inline lane
    is exactly the wrong answer: the run would execute twice and the caller would hear about the
    inline one. 503 with the derived id is the retryable answer — same key, same run.

    Serve is mocked but must never be called: the assertion is that NOTHING executed.
    """
    route = respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))
    client.app.state.workflow_scheduler = _RefusingScheduler(ScheduleUnconfirmed("the workflow engine neither started nor refused run run-x within 7s"))  # type: ignore[attr-defined]

    resp = client.post("/api/flows/runs", json=CHAIN, headers={"Idempotency-Key": "k-1"})

    assert resp.status_code == 503
    assert "retry with the same Idempotency-Key" in resp.json()["detail"]
    assert not route.called
    assert _runs(client) == {}  # a refused run leaves no resource behind


@respx.mock
def test_a_confirmed_refusal_still_degrades_to_the_inline_lane(client: TestClient) -> None:
    """The measured 2026-08-06 behaviour, pinned so the fix above cannot quietly take it away: when
    the scheduler has ESTABLISHED that no instance exists (a sidecar whose state store is not scoped
    to this app), the graph is still perfectly runnable here and now."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))
    client.app.state.workflow_scheduler = _RefusingScheduler(RuntimeError("the state store is not configured to use the actor runtime"))  # type: ignore[attr-defined]

    resp = client.post("/api/flows/runs", json=CHAIN)

    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


@respx.mock
def test_the_same_idempotency_key_resolves_to_the_same_run_and_executes_once(client: TestClient) -> None:
    """The retry-safety the schedule seam depends on: without a caller key there is nothing for a
    503'd client to converge on, and every retry would be a new run."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    first = client.post("/api/flows/runs", json=CHAIN, headers={"Idempotency-Key": "page-7"}).json()
    second = client.post("/api/flows/runs", json=CHAIN, headers={"Idempotency-Key": "page-7"}).json()

    assert first["run_id"] == second["run_id"]
    assert second == first  # the same document, not a re-execution
    assert respx.calls.call_count == 1
    assert len(_runs(client)) == 1


def test_a_key_less_post_still_gets_a_fresh_run_each_time(client: TestClient) -> None:
    """No key means nothing to converge ON. Inventing one would make every retry a new run while
    pretending otherwise, so the randomness stays in the key and the id derivation stays single."""
    scheduler = _RecordingScheduler()
    client.app.state.workflow_scheduler = scheduler  # type: ignore[attr-defined]

    ids = {client.post("/api/flows/runs", json=CHAIN).json()["run_id"] for _ in range(3)}

    assert len(ids) == 3
    assert len(scheduler.dispatched) == 3


def test_a_run_id_is_derived_from_the_subject_and_the_key() -> None:
    """Deterministic across processes — it is the workflow INSTANCE id, so a retry on another
    replica must land on the same instance. Scoped by subject, or one caller's `Idempotency-Key: 1`
    would resolve to another caller's run."""
    assert routes.run_id_for("alice", "k") == routes.run_id_for("alice", "k")
    assert routes.run_id_for("alice", "k") != routes.run_id_for("bob", "k")
    assert routes.run_id_for("alice", "k") != routes.run_id_for("alice", "other")
    assert routes.run_id_for("alice", "k").startswith("run-")


@respx.mock
def test_the_run_store_is_bounded_and_evicts_the_oldest(client: TestClient) -> None:
    """`max_runs` is a memory bound on a process-local dict, not a retention policy — stated because
    an unbounded dict on a long-lived pod is a leak that only shows up in production."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text=ALTO))

    ids = [client.post("/api/flows/runs", json=CHAIN).json()["run_id"] for _ in range(4)]

    assert len(_runs(client)) == 3  # max_runs from the fixture
    assert client.get(f"/api/flows/runs/{ids[0]}").status_code == 404
    assert client.get(f"/api/flows/runs/{ids[-1]}").status_code == 200


def test_an_oversized_graph_is_REFUSED_at_the_door_with_the_node_NAMED(client: TestClient) -> None:
    """The bound has to reach the 422 BODY, not just the pure function.

    `validate_graph`'s new size checks are worth nothing to a caller unless they arrive as
    `RunRefused.problems` — that list is what the builder paints, and a run refused with an empty
    list is indistinguishable from a run refused for a reason nobody can see.

    Driven at the fan-in bound rather than the node cap because it is the reachable one: two nodes
    and a repeatedly-dragged edge, which is how a real canvas produces it. No Serve mock is
    registered on purpose — a graph refused at the door must never reach the executor.
    """
    from flows.models import MAX_NODE_FAN_IN

    over = MAX_NODE_FAN_IN + 1
    resp = client.post(
        "/api/flows/runs",
        json={
            "graph": {
                "nodes": [{"id": "a", "kind": "text"}, {"id": "b", "kind": "inspect"}],
                "edges": [{"source": "a", "target": "b"} for _ in range(over)],
            },
            "seeds": {"a": "hello"},
        },
    )

    assert resp.status_code == 422, f"an over-wide graph was accepted: {resp.status_code}"
    body = resp.json()
    assert body["status"] == 422
    assert body["problems"], "refused with an EMPTY problems list — the builder has nothing to paint"
    assert any("b" in p and str(MAX_NODE_FAN_IN) in p for p in body["problems"]), f"the 422 must name the offending node AND the ceiling: {body['problems']}"


class _RecordingReader:
    """A run reader that records terminations. `state` answers None so the route falls to the cache."""

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def state(self, _run_id: str) -> dict[str, object] | None:
        return None

    def terminate(self, run_id: str) -> None:
        self.terminated.append(run_id)


def test_a_durable_run_can_be_TERMINATED(client: TestClient) -> None:
    """DWF-MGT-003: flows exposed start and status and no way to stop.

    `flow_run_workflow` fans out `run_node` activities against LIVE Ray Serve endpoints, so a wide
    graph aimed at a wedged Serve deployment occupies replicas for `serve_timeout` x NODE_RETRY per
    node per wave — and a pod restart does not help, because the instance is durable and resumes. The
    only way to stop it was to wait.

    A HARD terminate is right here and was NOT right for ingest, which is why this is not a copy of
    that fix: ingest's `terminate_workflow` skipped `emit_terminal`, the only caller of
    `release_run_units`, and stranded the run's JetStream consumer. flows holds no queue and no
    consumer — it fans out activities and returns — so there is no cleanup a skipped path could leak.
    """
    reader = _RecordingReader()
    client.app.state.workflow_reader = reader  # type: ignore[attr-defined]

    resp = client.post("/api/flows/runs/run-abc/terminate")

    assert resp.status_code == HTTPStatus.ACCEPTED
    assert reader.terminated == ["run-abc"]
    # The body states the SDK's real semantics rather than implying a completed stop: a caller told
    # "terminated" would reasonably free the Serve replicas in their head, and they are not free.
    assert "in flight" in resp.json()["detail"]


def test_terminate_without_an_engine_is_UNAVAILABLE_not_a_silent_success(client: TestClient) -> None:
    """No reader means no sidecar. Answering 202 would tell an operator the runaway was stopped."""
    client.app.state.workflow_reader = None  # type: ignore[attr-defined]

    resp = client.post("/api/flows/runs/run-abc/terminate")

    assert resp.status_code == HTTPStatus.SERVICE_UNAVAILABLE
