"""`train_run` was startable and then invisible: no status, no terminate (DWF-MGT-002/003).

`POST /train` returns 202 and schedules a durable watcher, and until this change there was no HTTP
means to learn whether that watcher was alive, had abandoned the run at its poll ceiling, or had never
been scheduled at all -- which, on the default chart, was what actually happened (see
`tests/unit/test_train_watch_is_hosted.py`, fixed in the same change).

Both routes are gated by the SAME door as `POST /train`. Reading the status of compute a caller was
refused permission to spend is not public, and the estate already argues exactly that on
`flows.get_run` and `ingest.get_ingest`.

Terminate is HARD and the body says what it does NOT do: `train_run` only polls a Ray job that
`submit_train_job` had already submitted before the watcher existed, so stopping the watch frees no
GPU. That is the opposite of ingest, where terminate had to become an event because the skipped tail
held the only caller of `release_run_units`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medallion.api import train as train_module
from service_kit.exceptions import register_handlers


class _State:
    def __init__(self, payload: dict[str, Any], status: WorkflowStatus) -> None:
        self.serialized_input = json.dumps(payload)
        self.runtime_status = status


class _Client:
    def __init__(self, instances: dict[str, _State] | None = None) -> None:
        self._instances = dict(instances or {})
        self.terminated: list[str] = []

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._instances.get(instance_id)

    def terminate_workflow(self, instance_id: str) -> None:
        self.terminated.append(instance_id)


LIVE = "train-ray-train-tok-1"


def _app(client: _Client | None) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(train_module.router)
    app.state.workflow_client = client
    # The door itself is exercised by `test_promotion_door` and the produce-auth suite; here it is
    # overridden so these tests isolate the ROUTE. Leaving it live would make every assertion below
    # depend on the auth posture rather than on the management surface under test.
    app.dependency_overrides[train_module.authorize_train] = lambda: "CiQwOGE4Njg0Yi1kYjg4"
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    live = _Client({LIVE: _State({"submission_id": "ray-train-tok-1", "token": "tok-1", "model": "churn"}, WorkflowStatus.RUNNING)})
    app = _app(live)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.app.state.workflow_client = live  # type: ignore[attr-defined]
        yield test_client


def test_a_live_training_watch_can_be_OBSERVED(client: TestClient) -> None:
    resp = client.get(f"/trains/{LIVE}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["submission_id"] == "ray-train-tok-1", "an operator must be able to cross-check the Ray dashboard"


def test_the_status_route_does_not_disclose_the_whole_spec(client: TestClient) -> None:
    """A declared field list, not the SDK state object -- which carries the serialized input and output."""
    body = client.get(f"/trains/{LIVE}").json()

    assert set(body) == {"instance_id", "status", "submission_id"}, f"the wire shape widened: {sorted(body)}"


def test_an_UNKNOWN_watch_is_404_not_a_silent_empty_state(client: TestClient) -> None:
    assert client.get("/trains/train-nope").status_code == 404


def test_a_live_watch_can_be_TERMINATED(client: TestClient) -> None:
    resp = client.post(f"/trains/{LIVE}/terminate")

    assert resp.status_code == 202, resp.text
    assert cast("Any", client.app).state.workflow_client.terminated == [LIVE]


def test_the_terminate_body_REFUSES_to_imply_the_ray_job_stopped(client: TestClient) -> None:
    """The whole reason this is worth a custom body. `train_run` polls a job it did not submit."""
    detail = client.post(f"/trains/{LIVE}/terminate").json()["detail"]

    assert "keeps running" in detail and "Ray" in detail, f"the body lets an operator believe the GPUs are free: {detail!r}"


def test_terminating_an_UNKNOWN_watch_is_404_and_terminates_NOTHING(client: TestClient) -> None:
    resp = client.post("/trains/train-nope/terminate")

    assert resp.status_code == 404
    assert cast("Any", client.app).state.workflow_client.terminated == []


@pytest.mark.parametrize("call", [lambda c: c.get(f"/trains/{LIVE}"), lambda c: c.post(f"/trains/{LIVE}/terminate")])
def test_no_engine_is_UNAVAILABLE_never_a_silent_success(call: Any) -> None:
    """503, not 404 and not 202. Answering 202 with no sidecar tells an operator a runaway was stopped."""
    with TestClient(_app(None), raise_server_exceptions=False) as client:
        assert call(client).status_code == 503
