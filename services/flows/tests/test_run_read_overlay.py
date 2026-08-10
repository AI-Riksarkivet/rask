"""`GET /flows/runs/{id}` must answer from the ENGINE, not from the process-local dict.

`test_run_reader.py` pins `_state_from_engine`, the pure mapping. These pin the ROUTE — the
precedence rule, which is where the defect actually lived. The pure function could be perfect and the
bug would survive if `get_run` still returned `runs.get(run_id)` first.

Three properties, and each corresponds to one way the durable lane was unreadable:
  * a COMPLETED run whose local record still says `running` must read as completed  — the stale record,
  * a run absent from the dict must still be readable                                — eviction / restart,
  * a run absent from BOTH must 404                                                  — not a silent 200.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from flows import routes
from flows.config import FlowsSettings
from flows.models import RunState
from service_kit.exceptions import register_handlers


RUN_ID = "run-abc"


class _Engine:
    """A `FlowRunReader` double. Structural, and `state` is SYNC exactly like the real adapter — the
    route drives it through `asyncio.to_thread`, so a coroutine here would pass a test the shipped
    code would fail."""

    def __init__(self, state: dict[str, object] | None) -> None:
        self._state = state
        self.calls = 0

    def state(self, run_id: str) -> dict[str, object] | None:
        self.calls += 1
        return self._state


def _app(engine: Any, runs: dict[str, RunState] | None = None) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.state.flows_settings = FlowsSettings(serve_url="http://serve.invalid:8000")
    app.state.http = httpx.AsyncClient()
    app.state.runs = runs if runs is not None else {}
    app.state.workflow_scheduler = None
    app.state.workflow_reader = engine
    return app


@pytest.fixture
def finished() -> Iterator[dict[str, object]]:
    yield {
        "runtime_status": "COMPLETED",
        "serialized_output": RunState(run_id=RUN_ID, status="succeeded").model_dump_json(),
    }


def test_the_engine_OVERRIDES_a_stale_local_record(finished: dict[str, object]) -> None:
    """THE regression. The durable lane writes `running` once and never updates it, so preferring the
    dict reported a finished run as running for the life of the pod."""
    stale = {RUN_ID: RunState(run_id=RUN_ID, status="running")}
    with TestClient(_app(_Engine(finished), stale)) as client:
        body = client.get(f"/api/flows/runs/{RUN_ID}").json()

    assert body["status"] == "succeeded", "the stale local record won — the durable lane is still write-only"


def test_a_run_EVICTED_from_the_dict_is_still_readable(finished: dict[str, object]) -> None:
    """`_remember` evicts past `max_runs`, and a restart empties the dict entirely. Neither may turn a
    live durable run into a 404 — that is the failure an operator hits watching a long run."""
    with TestClient(_app(_Engine(finished), {})) as client:
        response = client.get(f"/api/flows/runs/{RUN_ID}")

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_a_run_in_NEITHER_place_is_a_404_not_an_empty_200() -> None:
    """The fallback must stay reachable: an engine that has nothing is different from an engine that
    says running, and only the first may fall through to the dict — then to a genuine 404."""
    with TestClient(_app(_Engine(None), {})) as client:
        assert client.get(f"/api/flows/runs/{RUN_ID}").status_code == 404


def test_the_local_record_still_answers_when_there_is_NO_engine() -> None:
    """No sidecar is the inline lane, and it is a supported deployment (`make dev-micro`). With no
    reader the dict is the only record there is, and it must still serve."""
    inline = {RUN_ID: RunState(run_id=RUN_ID, status="succeeded")}
    with TestClient(_app(None, inline)) as client:
        response = client.get(f"/api/flows/runs/{RUN_ID}")

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_the_engine_is_consulted_exactly_ONCE_per_read(finished: dict[str, object]) -> None:
    """A status endpoint is polled. Asking the sidecar twice per request doubles that load for nothing,
    and it is the kind of regression a refactor introduces silently."""
    engine = _Engine(finished)
    with TestClient(_app(engine, {})) as client:
        client.get(f"/api/flows/runs/{RUN_ID}")

    assert engine.calls == 1
