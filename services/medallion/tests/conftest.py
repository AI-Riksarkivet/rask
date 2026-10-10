"""Shared fixtures for the medallion suite.

`compute_engine` is the out-of-process engine a planned run travels to (CP-056): compute's real app, its engine door
served over uvicorn on loopback and verifying the planner's projected token, with only the Ray dashboard's Jobs API
standing in.

`stub_stage_write_authorization` exists because a governance call was added to a path many of these tests drive
for other reasons. `_run_compute` now asks the catalog to authorize the stage's WRITE before anything
is dispatched (`catalog_register.authorize_stage_write`), which is a real HTTP request. Tests that
stub `ensure_stage_output` to drive the write path would otherwise fail on a connection error while
testing something else entirely.

DEFAULTED, NOT DISABLED. The stub answers what the live catalog answers today (`server_mediated`,
measured against `bind86-gold$catalog` with the stage runner's own credential), and it RECORDS its calls, so
a test that cares about the authorization can assert on it rather than re-stub. A recorded call
proves only that a stub was reached, never that production makes the call.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import BaseModel, ConfigDict

from medallion.services import catalog_register


#: The namespace the loopback issuer's tokens name, and each planner's account in it with the subject compute maps.
ENGINE_NAMESPACE = "rask"
PLANNER_ACCOUNTS = {"rask-sa-bronze-to-silver": "service-bronze-to-silver", "rask-sa-medallion-producer": "service-medallion-producer"}
#: The task table compute is deployed with: task -> the Jobs-API entrypoint.
ENGINE_TASKS = {"stage-transform": "python /home/ray/jobs/ray_stage_job.py", "train": "python /home/ray/jobs/ray_train_job.py"}


@pytest.fixture(autouse=True)
def stub_stage_write_authorization(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Answer the write-authorization call in-process; return the list of calls made."""
    calls: list[dict[str, Any]] = []

    def _authorize(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "server_mediated"

    monkeypatch.setattr(catalog_register, "authorize_stage_write", _authorize)
    return calls


class RayJobsApi:
    """The Ray dashboard's Jobs API as compute's engine door speaks to it: keeps each submission's body, answers a
    duplicate id 400 as Ray does, each job's scripted states in turn with its recorded cause, and 404 once a job's
    script is spent or it was never scripted: the head no longer holds it."""

    def __init__(self) -> None:
        self.bodies: dict[str, dict[str, Any]] = {}
        self.states: dict[str, list[str]] = {}
        self.causes: dict[str, dict[str, Any]] = {}
        self.stopped: list[str] = []

    @property
    def posted(self) -> list[str]:
        return list(self.bodies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs/":
            body = json.loads(request.content)
            submission = body["submission_id"]
            if submission in self.bodies:
                return httpx.Response(400, json={"detail": f"job {submission} already exists"})
            self.bodies[submission] = body
            return httpx.Response(200, json={"submission_id": submission})
        job_id = request.url.path.removeprefix("/api/jobs/")
        if request.method == "POST" and job_id.endswith("/stop"):
            self.stopped.append(job_id.removesuffix("/stop"))
            return httpx.Response(200, json={"stopped": True})
        scripted = self.states.get(job_id)
        if request.method == "GET" and scripted:
            return httpx.Response(200, json={"submission_id": job_id, "status": scripted.pop(0), **self.causes.get(job_id, {})})
        return httpx.Response(404)


class ComputeEngine(BaseModel):
    """The engine app a test's planners invoke: where it listens, the Jobs API behind it, and each planner's token."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    jobs: RayJobsApi
    token_dir: Path

    def token_file(self, account: str) -> str:
        """The projected `rask-compute` token a planner running as ``account`` presents, as the kubelet writes it."""
        return str(self.token_dir / f"{account}-rask-compute")

    def binding(self, account: str) -> dict[str, Any]:
        """The `MEDALLION_ENGINES` entry a planner running as ``account`` is deployed with."""
        return {"app_id": "compute", "url": self.url, "token_file": self.token_file(account), "capabilities": ["cancel", "failure_detail"]}


@pytest.fixture
def compute_engine(sa_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ComputeEngine]:
    """compute's real app over uvicorn, configured as the chart deploys it: its door maps each planner's account."""
    env = {
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-compute",
        "RASK_SA_SUBJECTS": json.dumps({f"system:serviceaccount:{ENGINE_NAMESPACE}:{sa}": subject for sa, subject in PLANNER_ACCOUNTS.items()}),
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        "RASK_COMPUTE_ENGINE_TASKS": json.dumps(ENGINE_TASKS),
        # The planner addresses the tests' settings name as their outcome door base.
        "RASK_COMPUTE_ENGINE_OUTCOME_BASES": json.dumps(["http://bronze-to-silver:8000", "http://rask-medallion-producer:8000"]),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from compute import app
    from compute.dependencies import get_compute_settings
    from compute.engine import get_jobs_api

    get_compute_settings.cache_clear()
    jobs = RayJobsApi()
    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(jobs.handle))
    app.dependency_overrides[get_jobs_api] = lambda: client
    token_dir = tmp_path / "engine-tokens"
    token_dir.mkdir()
    for account in [*PLANNER_ACCOUNTS, "rask-sa-gateway"]:
        (token_dir / f"{account}-rask-compute").write_text(sa_issuer.mint(account, audience="rask-compute", namespace=ENGINE_NAMESPACE))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("compute did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield ComputeEngine(url=f"http://127.0.0.1:{port}", jobs=jobs, token_dir=token_dir)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.pop(get_jobs_api, None)
        get_compute_settings.cache_clear()
        # The app is a module global, and its lifespan left this server's verifiers on it: a later test driving the
        # same app in-process would otherwise authenticate against an issuer that is gone.
        for attr in ("sa_oidc", "oidc", "fga"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)
