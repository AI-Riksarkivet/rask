"""A planned training run reaches exactly one terminal, and a running one none (CP-029 S2: clause c).

Driven on real collaborators end to end. The producer's own training consumer (`handle_train_trigger`) plans and
submits each run into a plan store on `tmp_path`; the REAL training job (`scripts/ray_train_job.py`) trains on real
pylance and stamps its registry commit with the run's marker; the plan sweep reads Ray through the real executor
adapter; and a late report reaches the outcome door of the producer's own app, running its own lifespan, which builds
the service-account verifier from the env the chart renders and checks the compute head's projected token offline
against the loopback issuer.

What stands in, and only that: the Dapr sidecar (a bus that records each publish) and the Ray dashboard (an httpx
transport answering each job's scripted state, and 404 once the head has lost the job).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from medallion.core.config import MedallionSettings, get_settings
from medallion.services import ray_submit, train, train_plans
from service_kit.lakehouse.run_outcomes import OutcomeReport
from service_kit.lakehouse.run_plans import PlanDocument


_JOB_PATH = Path(__file__).parents[3] / "scripts" / "ray_train_job.py"
NAMESPACE = "rask"
#: The compute head's one account, and the subject the chart maps it to at the producer's door.
HEAD_SA = "rask-sa-ray"
LINEAGE_TOPIC = "lineage.events.v1"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_train_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()


class _Bus:
    """The producer's Dapr client: records every publish, and closes at shutdown like the SDK's."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str = "application/json", publish_metadata: dict[str, str] | None = None
    ) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})

    async def close(self) -> None:
        return None

    def terminals(self, run_id: str) -> list[dict[str, Any]]:
        return [p["data"] for p in self.published if p["topic"] == LINEAGE_TOPIC and p["data"]["run"]["runId"] == run_id]


class _Dashboard:
    """The Ray Jobs API: accepts every submission, and answers each job's scripted states in turn, then 404."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.states: dict[str, list[str]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs/":
            submission = json.loads(request.content)["submission_id"]
            self.posted.append(submission)
            return httpx.Response(200, json={"submission_id": submission})
        job_id = request.url.path.removeprefix("/api/jobs/")
        scripted = self.states.get(job_id, [])
        if request.method == "GET" and scripted:
            return httpx.Response(200, json={"submission_id": job_id, "status": scripted.pop(0)})
        return httpx.Response(404)


@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> _Dashboard:
    fake = _Dashboard()
    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(fake.handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return fake


@pytest.fixture
def producer(tmp_path: Path, sa_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Bus, MedallionSettings]]:
    """The producer's own app, configured as the chart renders it with `medallion.ray` on, its sidecar the bus."""
    base = tmp_path / "medallion"
    lance.write_dataset(pa.table({"amount": [1.0, 3.0]}), str(base / "silver"))
    env = {
        "MEDALLION_RAY_ENABLED": "true",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
        "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
        "MEDALLION_BRONZE_URI": str(base / "bronze"),
        "MEDALLION_CONTROL_ROOT": str(tmp_path / "control"),
        "MEDALLION_LINEAGE_OUTBOX_URI": str(tmp_path / "outbox"),
        "MEDALLION_FGA_SERVICE_IDENTITY": "service-medallion-producer",
        "MEDALLION_PRODUCE_ADMIN_PROJECT": "acme",
        "MEDALLION_OUTCOME_URL_BASE": "http://rask-medallion-producer:8000",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-medallion",
        "RASK_SA_SUBJECTS": json.dumps({f"system:serviceaccount:{NAMESPACE}:{HEAD_SA}": "service-trainer"}),
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    from medallion import producer as producer_module

    bus = _Bus()
    monkeypatch.setattr(producer_module, "DaprClient", lambda: bus)
    monkeypatch.setattr(producer_module, "instrument_lance_if_available", lambda: None)
    monkeypatch.setattr(producer_module, "register_tasks", lambda _settings: None)
    try:
        with TestClient(producer_module.app, raise_server_exceptions=False) as client:
            yield client, bus, get_settings()
    finally:
        get_settings.cache_clear()


def _plan(settings: MedallionSettings, bus: _Bus, token: str) -> PlanDocument:
    """The producer's training consumer on one trigger: it plans the run, submits it and acks."""
    trigger = {"token": token, "model": "churn", "features": [{"dataset": "silver$features", "version": 1}], "originator": "alice"}
    assert asyncio.run(train.handle_train_trigger(settings, {"data": trigger}, dapr=bus)) == {"status": "SUCCESS"}
    plan = train_plans.plan_store(settings).read(ray_submit.train_submission_id(token))
    assert plan is not None
    return plan


def _job_commits_and_reaches_nobody(plan: PlanDocument, monkeypatch: pytest.MonkeyPatch) -> int:
    """The real training job, as its submission tells it to run, with lineage unreachable from the head: it trains and
    commits its marked registry version, and its COMPLETE never lands, so it reports nothing."""
    settings = get_settings()
    feature = {"dataset": "silver$features", "version": 1, "uri": train.feature_uri_for(settings, "silver$features")}
    with monkeypatch.context() as patch:
        patch.setenv("MODEL", "churn")
        patch.setenv("TRAIN_TOKEN", str(plan.trigger["token"]))
        patch.setenv("FEATURES", json.dumps([feature]))
        patch.setenv("REGISTRY_URI", plan.to_uri)
        patch.setenv("ARTIFACT_BASE", train.artifact_base_for(settings, "churn"))
        patch.setenv("RASK_IDEMPOTENCY_KEY", plan.action_id)
        patch.setenv("RASK_OUTCOME_URL", plan.report_url)
        patch.setattr(job, "emit", lambda _event: False)
        job.main()
    return int(lance.dataset(plan.to_uri).version)


def _report(client: TestClient, sa_issuer: Any, plan: PlanDocument, report: OutcomeReport) -> httpx.Response:
    """What `run_outcomes.report_outcome` sends from the job: the report, under the head's projected token."""
    token = sa_issuer.mint(HEAD_SA, audience="rask-medallion", namespace=NAMESPACE)
    return client.post(f"/runs/{plan.action_id}/outcome", json=report.model_dump(exclude_none=True), headers={"Authorization": f"Bearer {token}"})


def test_a_vanished_training_job_reaches_one_terminal_and_a_running_one_none(
    producer: tuple[TestClient, _Bus, MedallionSettings], dashboard: _Dashboard, sa_issuer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clause c. Three training runs are planned; the head then restarts and loses two of them.

    The one whose job never committed gets exactly one FAIL, bare. The one whose job committed its registry version and
    then could not reach lineage gets exactly one COMPLETE naming that version, read off its commit marker. The one
    still training, 25 hours in and past the poll ceiling the watcher used to have, gets nothing at all. Neither lost
    job is resubmitted, and a late report from either cannot add a second terminal.
    """
    client, bus, settings = producer
    lost = _plan(settings, bus, "tok-lost")
    landed = _plan(settings, bus, "tok-landed")
    training = _plan(settings, bus, "tok-training")
    committed = _job_commits_and_reaches_nobody(landed, monkeypatch)
    store = train_plans.plan_store(settings)
    store.update(training.action_id, lambda p: p.model_copy(update={"submitted_at": p.submitted_at - timedelta(hours=25)}))
    dashboard.states = {lost.action_id: ["RUNNING"], landed.action_id: ["RUNNING"], training.action_id: ["RUNNING"] * 3}

    for _ in range(3):
        report = asyncio.run(train_plans.sweep(settings, bus))
        assert report.errors == 0, report

    lost_terminals = bus.terminals(lost.run_id)
    assert [e["eventType"] for e in lost_terminals] == ["FAIL"], "a lost job that committed nothing must reach exactly one FAIL"
    fail = lost_terminals[0]
    assert "version" not in fail["outputs"][0].get("facets", {}), "a run that wrote nothing named a version"
    assert "vanished, and its registry carries no commit marker" in fail["run"]["facets"]["errorMessage"]["message"]
    assert fail["run"]["facets"]["lance"]["originator"] == "alice"
    landed_terminals = bus.terminals(landed.run_id)
    assert [e["eventType"] for e in landed_terminals] == ["COMPLETE"], "a lost job whose commit landed must reach exactly one COMPLETE"
    complete = landed_terminals[0]
    assert complete["outputs"][0]["name"] == "models$churn"
    assert complete["outputs"][0]["facets"]["version"]["datasetVersion"] == str(committed)
    assert bus.terminals(training.run_id) == [], "a job still running was given a terminal"
    still = store.read(training.action_id)
    assert still is not None and still.outcome is None
    assert dashboard.posted == [lost.action_id, landed.action_id, training.action_id], "a lost training job was submitted again"

    refused = _report(client, sa_issuer, lost, OutcomeReport(status="succeeded"))
    repeated = _report(client, sa_issuer, landed, OutcomeReport(status="succeeded", committed_version=committed))

    assert (refused.status_code, repeated.status_code) == (409, 200), (refused.text, repeated.text)
    assert (len(bus.terminals(lost.run_id)), len(bus.terminals(landed.run_id))) == (1, 1)
