"""`train_run` was startable and then invisible: no status, no terminate (DWF-MGT-002/003).

`POST /train` returns 202 and schedules a durable watcher, and until this change there was no HTTP
means to learn whether that watcher was alive, had abandoned the run at its poll ceiling, or had never
been scheduled at all -- which, on the default chart, was what actually happened (see
`tests/unit/test_train_watch_is_hosted.py`, fixed in the same change).

Both routes are gated on `can_administer` over the project the watch records. Reading the status of
compute a caller may not spend is not public, and the estate already argues exactly that on
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
from medallion.api.produce_auth import ProducerCaller, admit_caller
from service_kit.exceptions import register_handlers


class _State:
    """`WorkflowState`'s fields these routes read. ``name`` is the registered workflow, as the SDK's
    own state proxies it from the orchestration."""

    def __init__(self, payload: dict[str, Any], status: WorkflowStatus, *, name: str = "train_run") -> None:
        self.serialized_input = json.dumps(payload)
        self.runtime_status = status
        self.name = name


class _Client:
    def __init__(self, instances: dict[str, _State] | None = None) -> None:
        self._instances = dict(instances or {})
        self.terminated: list[str] = []
        self.read: list[str] = []

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        self.read.append(instance_id)
        return self._instances.get(instance_id)

    def terminate_workflow(self, instance_id: str) -> None:
        self.terminated.append(instance_id)


LIVE = "train-ray-train-tok-1"


def _app(client: _Client | None) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(train_module.router)
    app.state.workflow_client = client
    # The door and the per-watch authorization are exercised by
    # `test_the_operator_doors_authorize_on_the_resource.py`; here a caller the door decided whole
    # isolates the ROUTE, so every assertion below is about the management surface, not the auth posture.
    app.dependency_overrides[admit_caller] = ProducerCaller
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    live = _Client({LIVE: _State({"submission_id": "ray-train-tok-1", "token": "tok-1", "model": "churn"}, WorkflowStatus.RUNNING)})
    app = _app(live)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.app.state.workflow_client = live
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


# ── a training door acts on training watches only ───────────────────────────────────────────────────

#: A held promotion, hosted by the same app-id as the training watches. `/promotions/{id}` is its door,
#: gated on `can_promote`; a training door that acted on it would skip that rung and the outcome event.
PROMOTION = "promotion-tok-of-tenant-beta"
#: A `train-` id whose instance is not a training watch: the prefix alone cannot decide the kind.
FORGED = "train-forged"


@pytest.fixture
def hosting() -> Iterator[tuple[TestClient, _Client]]:
    promotion = {"token": "tok", "project": "beta", "from_namespace": "silver", "from_dataset": "silver$x", "to_namespace": "gold", "to_dataset": "gold$x"}
    engine = _Client(
        {
            LIVE: _State({"submission_id": "ray-train-tok-1", "token": "tok-1", "model": "churn"}, WorkflowStatus.RUNNING),
            PROMOTION: _State(promotion, WorkflowStatus.RUNNING, name="promotion_review"),
            FORGED: _State(promotion, WorkflowStatus.RUNNING, name="promotion_review"),
        }
    )
    with TestClient(_app(engine), raise_server_exceptions=False) as test_client:
        yield test_client, engine


@pytest.mark.parametrize("instance_id", [PROMOTION, FORGED])
def test_a_non_training_instance_is_404_and_is_NEVER_TERMINATED(hosting: tuple[TestClient, _Client], instance_id: str) -> None:
    client, engine = hosting

    shown = client.get(f"/trains/{instance_id}")
    stopped = client.post(f"/trains/{instance_id}/terminate")

    assert (shown.status_code, stopped.status_code) == (404, 404), (shown.text, stopped.text)
    assert engine.terminated == [], "a training door stopped a workflow that is not a training watch"


def test_an_id_a_training_watch_cannot_have_is_refused_BEFORE_the_engine_is_asked(hosting: tuple[TestClient, _Client]) -> None:
    """`schedule_train_watch` mints `train-<submission id>`, so any other id names no training watch and
    the promotion's persisted spec is never read to answer it."""
    client, engine = hosting

    assert client.get(f"/trains/{PROMOTION}").status_code == 404
    assert engine.read == []


def test_a_training_watch_is_still_served_beside_the_promotions(hosting: tuple[TestClient, _Client]) -> None:
    client, engine = hosting

    assert client.post(f"/trains/{LIVE}/terminate").status_code == 202
    assert engine.terminated == [LIVE]
