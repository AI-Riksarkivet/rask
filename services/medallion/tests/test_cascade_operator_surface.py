"""An in-flight `stage_run` could be neither observed nor stopped by any HTTP means (DWF-MGT-002/003).

`stage_run` submits a Ray job and watches it for up to `MAX_POLLS`, and until this change there was no
route anywhere that could report on one or stop one. `services/compute` proxies Ray read-only and
knows nothing about the workflow doing the watching, and the stage runners mounted only `/healthz` and their
two event doors.

THE SPLIT IS FORCED, NOT CHOSEN, and these tests pin both halves because either alone is useless:

  * `terminate_workflow` and `get_workflow_state` resolve an instance through the CALLING app's
    app-id, and `stage_run` executes in the STAGE RUNNER's runtime -- so the routes that touch the workflow
    must live there. A producer-hosted copy would look under `medallion-producer`, find nothing, and
    ACCEPT THE CALL ANYWAY: a 202 for a terminate that stopped nothing. `promotions.py` records that
    exact trap from the other direction.
  * a stage runner is bus-only -- no gateway row, no Ingress -- so a route hosted only there is a lever
    nobody can pull.

So the producer authorizes and forwards, and the stage runner does the work under its own app-id.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medallion.api import service_door, stage_ops
from service_kit.exceptions import register_handlers


LIVE = "stage-ray-silver-tok-1"


class _State:
    def __init__(self, payload: dict[str, Any], status: WorkflowStatus) -> None:
        self.serialized_input = json.dumps(payload)
        self.runtime_status = status


class _Client:
    def __init__(self, instances: dict[str, _State]) -> None:
        self._instances = instances
        self.terminated: list[str] = []

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._instances.get(instance_id)

    def terminate_workflow(self, instance_id: str) -> None:
        self.terminated.append(instance_id)


def _stage_runner_app(client: _Client | None) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(stage_ops.router)
    app.state.workflow_client = client
    # The producer-only door has its own suite (`test_the_operator_doors_authorize_on_the_resource.py`);
    # overridden so these assertions are about the management surface rather than the auth posture.
    app.dependency_overrides[service_door.require_producer] = lambda: None
    return app


@pytest.fixture
def stage_runner() -> Iterator[TestClient]:
    live = _Client({LIVE: _State({"submission_id": "ray-silver-tok-1", "polls_done": 4, "from_uri": "s3://wh/secret"}, WorkflowStatus.RUNNING)})
    with TestClient(_stage_runner_app(live), raise_server_exceptions=False) as c:
        yield c


def test_a_live_cascade_stage_can_be_OBSERVED(stage_runner: TestClient) -> None:
    body = stage_runner.get(f"/stages/{LIVE}").json()

    assert body["status"] == "RUNNING"
    assert body["submission_id"] == "ray-silver-tok-1", "an operator must be able to cross-check the Ray dashboard"
    assert body["polls_done"] == 4


def test_a_live_cascade_stage_can_be_TERMINATED(stage_runner: TestClient) -> None:
    resp = stage_runner.post(f"/stages/{LIVE}/terminate")

    assert resp.status_code == 202, resp.text
    assert cast("Any", stage_runner.app).state.workflow_client.terminated == [LIVE]


@pytest.mark.parametrize("call", [lambda c: c.post("/stages/nope/terminate")])
def test_an_UNKNOWN_instance_is_404_on_both_verbs(stage_runner: TestClient, call: Any) -> None:
    assert call(stage_runner).status_code == 404


@pytest.mark.parametrize("call", [lambda c: c.get(f"/stages/{LIVE}"), lambda c: c.post(f"/stages/{LIVE}/terminate")])
def test_no_engine_is_UNAVAILABLE_never_a_silent_success(call: Any) -> None:
    """503, not 202. A stage_runner with no runtime cannot have the instance, and answering 202 would tell an
    operator a runaway was stopped when nothing was called."""
    with TestClient(_stage_runner_app(None), raise_server_exceptions=False) as c:
        assert call(c).status_code == 503
