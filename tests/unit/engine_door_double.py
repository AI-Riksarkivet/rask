"""The engine a planner invokes, in-process: compute's REAL engine router over ASGI, with Ray's Jobs API a transport.

For a unit-tier test whose claim is what a planner sends or how it answers an engine outcome, and which has no
loopback issuer or live server to spare: the planner's invoked executor (`engine_registry`) reaches compute's own
`/engine/runs` routes, which submit through the real `ray_kit.jobs_api` kernel to ``ray_handler``. Authentication is
off on both sides, as the root conftest acknowledges; the door's admission is the live keeper's
(`services/medallion/tests/conftest.py::compute_engine`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


#: compute's task table for the `ray` engine in these tests, as the chart deploys it.
ENGINE_TASKS = {
    "stage-transform": "python /home/ray/jobs/ray_stage_job.py",
    "dummy-lane": "python /home/ray/jobs/ray_dummy_job.py",
    "train": "python /home/ray/jobs/ray_train_job.py",
}

#: A planner's `MEDALLION_ENGINES` for compute, reached at a URL the double answers.
ENGINES: dict[str, Any] = {"ray": {"app_id": "compute", "url": "http://compute", "tasks": [{"task": task} for task in ENGINE_TASKS]}}


def route_engine_to_compute(monkeypatch: pytest.MonkeyPatch, ray_handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Point every invoked engine at compute's real app in-process, its Jobs API answered by ``ray_handler``."""
    from compute import app
    from compute.config import ComputeSettings
    from compute.dependencies import get_compute_settings
    from compute.engine import get_jobs_api
    from medallion.services import engine_registry

    ray = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(ray_handler))
    settings = ComputeSettings.model_validate({"RASK_COMPUTE_ENGINE_TASKS": {"ray": ENGINE_TASKS}, "RASK_INSECURE_ALLOW_UNAUTHENTICATED": True})
    monkeypatch.setitem(app.dependency_overrides, get_jobs_api, lambda: ray)
    monkeypatch.setitem(app.dependency_overrides, get_compute_settings, lambda: settings)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://compute")
    monkeypatch.setattr(engine_registry, "_engine_client", lambda _timeout: client)
