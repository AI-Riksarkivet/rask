"""compute's engine door admits from a planner only what a planner may decide (CP-056).

The order crosses a trust boundary into the engine app, and Ray lays a job's `runtime_env.env_vars` over its pod's,
so whatever variable the order may name, the caller chooses for the job's process. Measured before this door
refused it: an order carrying `PYTHONWARNINGS` and `BROWSER` in its OTLP map runs a shell command on Python 3.13, one
carrying `S3_ENDPOINT` or `RASK_LINEAGE_ENDPOINT` overrides the pod's own, and an outcome URL on the caller's host
receives the job's projected token. A task resolves only through the table of the engine the submission names, so
another engine's command never runs as this one's. Each shape below is refused 422 and nothing reaches Ray.

Driven at the real app's door in-process; Ray's Jobs API is a transport that records every call. Authentication is
not the claim (the live keeper `services/medallion/tests/conftest.py::compute_engine` drives the token).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from compute import app
from compute.config import ComputeSettings
from compute.dependencies import get_compute_settings
from compute.engine import get_jobs_api


_KEY = "0123456789abcdef0123456789abcdef01234567"
_PLANNER = "http://rask-bronze-to-silver:8000"


def _order(**over: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "task": "stage-transform",
        "source": {"uri": "s3://lake/bronze", "table_id": "bronze$events"},
        "destination": {"uri": "s3://lake/silver", "table_id": "silver$features"},
        "stamp": {"stage": "silver", "cardinality": "1:1"},
        "identity": {"run_id": "run-1"},
        "idempotency_key": _KEY,
        "outcome_url": f"{_PLANNER}/runs/{_KEY}/outcome",
    }
    return order | over


@pytest.fixture
def door() -> Iterator[tuple[TestClient, list[httpx.Request]]]:
    calls: list[httpx.Request] = []

    def ray(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"submission_id": _KEY, "status": "RUNNING"})

    settings = ComputeSettings.model_validate(
        {
            "RASK_COMPUTE_ENGINE_TASKS": {
                "ray": {"stage-transform": "python /home/ray/jobs/ray_stage_job.py"},
                "spark": {"spark-only": "spark-submit job.py"},
            },
            "RASK_COMPUTE_ENGINE_OUTCOME_BASES": [_PLANNER],
            "RASK_INSECURE_ALLOW_UNAUTHENTICATED": True,
        }
    )
    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(ray))
    app.dependency_overrides[get_compute_settings] = lambda: settings
    app.dependency_overrides[get_jobs_api] = lambda: client
    try:
        yield TestClient(app, raise_server_exceptions=False), calls
    finally:
        app.dependency_overrides.pop(get_compute_settings, None)
        app.dependency_overrides.pop(get_jobs_api, None)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param(
            "POST",
            "/engine/runs",
            {"engine": "ray", "order": _order(observability={"otlp": {"PYTHONWARNINGS": "all:0:antigravity.x:0:0", "BROWSER": "/bin/sh -c id #%s"}})},
            id="a-variable-of-the-callers-choosing",
        ),
        pytest.param("POST", "/engine/runs", {"engine": "ray", "order": _order(params={"S3_ENDPOINT=x": "y"})}, id="a-param-that-is-no-variable-name"),
        pytest.param(
            "POST", "/engine/runs", {"engine": "ray", "order": _order(outcome_url=f"http://attacker:8000/runs/{_KEY}/outcome")}, id="an-outcome-door-elsewhere"
        ),
        pytest.param(
            "POST", "/engine/runs", {"engine": "ray", "order": _order(outcome_url=f"{_PLANNER}/runs/{'f' * 40}/outcome")}, id="another-runs-outcome-door"
        ),
        pytest.param(
            "POST", "/engine/runs", {"engine": "ray", "order": _order(idempotency_key="raysubmit_user_job", outcome_url="")}, id="a-job-no-planner-names"
        ),
        pytest.param("POST", "/engine/runs", {"engine": "flink", "order": _order()}, id="an-engine-this-door-does-not-serve"),
        pytest.param("POST", "/engine/runs", {"engine": "ray", "order": _order(task="spark-only")}, id="another-engines-task"),
        pytest.param("POST", "/engine/runs/raysubmit_user_job/cancel", None, id="stopping-a-job-no-planner-submitted"),
        pytest.param("GET", "/engine/runs/raysubmit_user_job/failure", None, id="reading-a-job-no-planner-submitted"),
    ],
)
def test_the_door_refuses_what_its_caller_may_not_decide(door: tuple[TestClient, list[httpx.Request]], method: str, path: str, body: Any) -> None:
    client, calls = door

    refused = client.request(method, path, json=body)

    assert refused.status_code == 422, refused.text
    assert calls == [], f"the refused request reached Ray: {[(c.method, str(c.url)) for c in calls]}"
