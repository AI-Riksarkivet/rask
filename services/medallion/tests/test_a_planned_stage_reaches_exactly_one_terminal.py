"""A planned Ray stage run reaches exactly one terminal (CP-029 S1: clauses b, d and e, and D-6's resubmit budget).

Driven on real collaborators end to end. The stage runner's own pass 1 (`handle_stage`) plans and submits the run into
a plan store on `tmp_path`; the REAL Ray stage job (`scripts/ray_stage_job.py`) writes the destination on real pylance
and stamps its commit marker; a job's report reaches the real outcome door (`api/stage_outcomes`) through a FastAPI
app running its lifespan, with the compute head's projected token verified offline against the loopback issuer; the
sweep resolves what no report closed; and pass 2 is `handle_stage` again on the trigger the hand-off published.

What stands in, and only that: the Dapr sidecar (a bus that records each publish and can refuse a topic), the Ray
dashboard that accepts pass 1's submission (an httpx transport), and the engine the sweep reads status from (the
executor port, one scripted state per tick and the failure Ray recorded).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import lance
import pytest
from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medallion.api.stage_outcomes import mount_stage_outcomes
from medallion.core.config import MedallionSettings, get_settings
from medallion.services import ray_submit, stage_plans
from medallion.services.compute import seed_bronze
from medallion.services.transform import handle_stage
from service_kit.exceptions import register_handlers
from service_kit.governed.machine_identity import ServiceAccountVerifier
from service_kit.lakehouse.commit_marker import CommitMarker
from service_kit.lakehouse.executor import Capability, Executor, RunFailure, RunHandle, RunState, SubmitOutcome
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.outbox import list_events
from service_kit.lakehouse.run_outcomes import OutcomeReport
from service_kit.lakehouse.run_plans import PlanDocument
from service_kit.lakehouse.stage_stamp import ONE_TO_ONE
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkOrder


_JOB_PATH = Path(__file__).parents[3] / "scripts" / "ray_stage_job.py"
NAMESPACE = "rask"
#: The compute head's one account, and the subject the chart maps it to at a stage runner's door.
HEAD_SA = "rask-sa-ray"
HEAD_SUBJECT = "service-trainer"
LINEAGE_TOPIC = "lineage.events.v1"
OWN_TOPIC = "medallion.bronze"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_stage_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()


class _Bus:
    """The Dapr client a stage runner holds: records every publish, and refuses the topics in ``refuse``."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.refuse: set[str] = set()

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str = "application/json", publish_metadata: dict[str, str] | None = None
    ) -> None:
        if topic_name in self.refuse:
            raise RuntimeError(f"the sidecar refused {topic_name}")
        self.published.append({"topic": topic_name, "data": json.loads(data)})

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [p["data"] for p in self.published if p["topic"] == topic]


class _Engine:
    """The executor port as the sweep reads it: one scripted state per status read, every submit recorded, and the
    failure Ray recorded for the job, when it recorded one."""

    name = "ray"
    capabilities = frozenset({Capability.CANCEL, Capability.FAILURE_DETAIL})

    def __init__(self, *states: RunState, failure: RunFailure | None = None) -> None:
        self._states = list(states)
        self._failure = failure
        self.submitted: list[str] = []

    def validate_task(self, registration: TaskRegistration) -> None:
        return None

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        self.submitted.append(order.idempotency_key)
        return RunHandle(engine=self.name, handle=order.idempotency_key), SubmitOutcome.SUBMITTED

    async def status(self, handle: RunHandle) -> RunState:
        return self._states.pop(0)

    async def failure(self, handle: RunHandle) -> RunFailure | None:
        return self._failure

    async def cancel(self, handle: RunHandle) -> None:
        return None

    async def result(self, handle: RunHandle) -> Any:
        raise NotImplementedError


@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Ray Jobs API pass 1 submits through: accepts every submission and records its id."""
    posted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs/":
            sub_id = json.loads(request.content)["submission_id"]
            posted.append(sub_id)
            return httpx.Response(200, json={"submission_id": sub_id})
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return posted


@pytest.fixture
def settings(tmp_path: Path) -> MedallionSettings:
    bronze, silver = tmp_path / "bronze", tmp_path / "silver"
    seed_bronze(str(bronze), {}, rows=4)
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "ray_enabled": True,
            "control_root": str(tmp_path / "control"),
            "lineage_outbox_uri": str(tmp_path / "outbox"),
            "from_uri": str(bronze),
            "to_uri": str(silver),
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed_features",
            "sub_topic": OWN_TOPIC,
            "fga_service_identity": "service-bronze-to-silver",
            "outcome_url_base": "http://bronze-to-silver:8000",
        }
    )


def _pass_one(settings: MedallionSettings, bus: _Bus, trigger: dict[str, Any]) -> PlanDocument:
    """The stage runner's first delivery: it plans the run, submits it and acks."""
    assert asyncio.run(handle_stage(cast(DaprClient, bus), settings, {"data": trigger})) == {"status": "SUCCESS"}
    store = stage_plans.plan_store(settings)
    ((action_id, _written),) = store.open_entries()
    plan = store.read(action_id)
    assert plan is not None
    return plan


def _job_commits(plan: PlanDocument, *, cardinality: str = ONE_TO_ONE) -> None:
    """The real stage job, as the order tells it to run: its last commit carries the run's marker."""
    order = plan.order
    assert order is not None
    job._run_stage(
        order.source.uri,
        order.destination.uri,
        order.stamp.stage,
        {},
        lineage=order.stamp.lineage_document,
        cardinality=cardinality,
        dataset_id=order.destination.table_id,
        marker=CommitMarker(action_id=plan.action_id, run_id=plan.run_id),
    )


def _sweep(settings: MedallionSettings, bus: _Bus, engine: _Engine) -> None:
    asyncio.run(stage_plans.sweep(settings, bus, executor=cast(Executor, engine)))


def _plan(settings: MedallionSettings, plan: PlanDocument) -> PlanDocument:
    current = stage_plans.plan_store(settings).read(plan.action_id)
    assert current is not None
    return current


@pytest.fixture
def door(settings: MedallionSettings, sa_issuer: Any) -> Iterator[tuple[TestClient, _Bus]]:
    """The stage runner's outcome door, its lifespan holding the bus and the service-account verifier the chart maps."""
    bus = _Bus()
    verifier = ServiceAccountVerifier(
        sa_issuer.issuer,
        "rask-medallion",
        {
            f"system:serviceaccount:{NAMESPACE}:{HEAD_SA}": HEAD_SUBJECT,
            f"system:serviceaccount:{NAMESPACE}:rask-sa-medallion-producer": "service-medallion-producer",
        },
        cache_ttl=60,
        leeway=60,
        fetch_token_file=str(sa_issuer.fetch_token_file),
        ca_file=str(sa_issuer.ca_file),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.dapr = bus
        app.state.sa_oidc = verifier
        yield

    app = FastAPI(lifespan=lifespan)
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    mount_stage_outcomes(app, sweep_binding_name="")
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, bus


def _report(client: TestClient, sa_issuer: Any, plan: PlanDocument, report: OutcomeReport) -> httpx.Response:
    """What `run_outcomes.report_outcome` sends from the job: the report, under the head's projected token."""
    token = sa_issuer.mint(HEAD_SA, audience="rask-medallion", namespace=NAMESPACE)
    return client.post(f"/runs/{plan.action_id}/outcome", json=report.model_dump(exclude_none=True), headers={"Authorization": f"Bearer {token}"})


def test_an_unnotified_run_publishes_no_FAIL_and_keeps_its_input_to_output_edge(
    settings: MedallionSettings, dashboard: list[str], door: tuple[TestClient, _Bus], sa_issuer: Any
) -> None:
    """Clause b. The job SUCCEEDED and its data landed, but the pass-2 hand-off could not be published.

    The run stays open and no failure is recorded for it; the next sweep tick, with Ray still saying SUCCEEDED, hands
    it off, and pass 2 emits the COMPLETE carrying the input->output edge at the version the job committed.
    """
    client, bus = door
    plan = _pass_one(settings, bus, {"token": "tok-1"})
    _job_commits(plan)
    committed = lance.dataset(settings.to_uri).version
    bus.refuse = {OWN_TOPIC}

    refused = _report(client, sa_issuer, plan, OutcomeReport(status="succeeded", committed_version=committed))

    assert refused.status_code == 503, refused.text
    assert _plan(settings, plan).outcome is None, "a run whose hand-off was not published must stay open"
    bus.refuse = set()
    _sweep(settings, bus, _Engine(RunState.SUCCEEDED))
    assert _plan(settings, plan).outcome is not None
    (handed_off,) = bus.on(OWN_TOPIC)
    assert asyncio.run(handle_stage(cast(DaprClient, bus), settings, {"data": handed_off})) == {"status": "SUCCESS"}
    kinds = [event["eventType"] for event in bus.on(LINEAGE_TOPIC)]
    assert "FAIL" not in kinds, f"a run Ray says SUCCEEDED was recorded as a failure: {kinds}"
    complete = next(event for event in bus.on(LINEAGE_TOPIC) if event["eventType"] == "COMPLETE")
    assert [i["name"] for i in complete["inputs"]] == ["bronze$events"]
    assert complete["outputs"][0]["name"] == "silver$features"
    assert complete["outputs"][0]["facets"]["version"]["datasetVersion"] == str(committed)
    # R26's one instant: the document the job wrote INTO the dataset and the COMPLETE name the same eventTime.
    written = json.loads(lance.dataset(settings.to_uri, version=committed).to_table(columns=["lineage"]).column("lineage")[0].as_py())
    assert complete["eventTime"] == written["event_time"]


def test_an_abandoned_stage_whose_destination_carries_the_marker_wakes_the_next_tier(
    settings: MedallionSettings, dashboard: list[str], door: tuple[TestClient, _Bus]
) -> None:
    """Clause d. The job landed and reported nothing, and the head then lost its record (seen RUNNING, then unknown).

    The destination's Lance history is the durable record: the marker above the plan's base version resolves the run
    succeeded, and its pass-2 trigger names the committed version, so the stage runner promotes. Nothing is resubmitted.
    """
    _client, bus = door
    plan = _pass_one(settings, bus, {"token": "tok-1"})
    _job_commits(plan)
    engine = _Engine(RunState.RUNNING, RunState.UNKNOWN)

    _sweep(settings, bus, engine)
    _sweep(settings, bus, engine)

    closed = _plan(settings, plan)
    assert closed.outcome is not None and closed.outcome.status == "succeeded"
    (handed_off,) = bus.on(OWN_TOPIC)
    assert handed_off["ray_job_done"] is True
    assert handed_off["ray_committed_version"] == lance.dataset(settings.to_uri).version
    assert engine.submitted == [], "a run whose marker landed was submitted again"


def test_a_job_that_commits_and_then_fails_names_the_version_its_marker_records(
    settings: MedallionSettings, dashboard: list[str], door: tuple[TestClient, _Bus], sa_issuer: Any, tmp_path: Path
) -> None:
    """Clause e. A tenant's run: the job's last commit landed, then its contract check failed.

    Ray says FAILED before the job's own report arrives, so the sweep resolves the run. The FAIL names the version the
    marker records (the WROTE edge with its version) on the tables as the tenant's grants name them, carries Ray's own
    cause bounded to the event's budget and the person the cascade is for, and goes through the lineage outbox: with
    the bus refusing the publish, the staged copy is the record. The job's report, arriving after, is answered with the
    recorded outcome and records nothing more.
    """
    warehouse = tmp_path / "acme-wh"
    seed_bronze(str(warehouse / "medallion" / "bronze"), {}, rows=4)
    registry = Path(settings.control_root) / "_warehouses"
    registry.mkdir(parents=True)
    (registry / "wh-acme.json").write_text(json.dumps({"id": "wh-acme", "project": "acme", "root_uri": str(warehouse), "status": "active"}))
    client, bus = door
    plan = _pass_one(settings, bus, {"token": "tok-1", "originator": "alice", "project": "acme"})
    with pytest.raises(SystemExit, match="unknown stage cardinality"):
        _job_commits(plan, cardinality="not-a-cardinality")
    committed = lance.dataset(plan.to_uri).version
    bus.refuse = {LINEAGE_TOPIC}
    traceback = "Traceback (most recent call last): SystemExit: unknown stage cardinality 'not-a-cardinality'" + " in frame" * 300

    _sweep(settings, bus, _Engine(RunState.FAILED, failure=RunFailure(kind="driver_error", message=traceback, exit_code=1)))
    late = _report(client, sa_issuer, plan, OutcomeReport(status="failed", error="SystemExit: unknown stage cardinality"))

    assert late.status_code == 200, late.text
    assert late.json()["committed_version"] == committed
    assert late.json()["source"] == "sweep", "the job's late report replaced the outcome the sweep recorded"
    staged = [json.loads(event) for _key, event in list_events(settings.lineage_outbox_uri, {})]
    fails = [event for event in staged if event["eventType"] == "FAIL"]
    assert len(fails) == 1, f"one run, one FAIL: {len(fails)}"
    (fail,) = fails
    assert [i["name"] for i in fail["inputs"]] == ["acme-bronze$events"], fail["inputs"]
    assert fail["outputs"][0]["name"] == "acme-silver$features", "the FAIL names a table the tenant's grants do not"
    wrote = fail["outputs"][0].get("facets", {}).get("version", {}).get("datasetVersion")
    assert wrote == str(committed), f"the FAIL of a run that committed v{committed} names version {wrote!r}"
    assert fail["run"]["facets"]["lance"]["originator"] == "alice"
    message = fail["run"]["facets"]["errorMessage"]["message"]
    assert "driver_error: Traceback" in message and "unknown stage cardinality" in message, f"Ray's cause did not reach the FAIL: {message[:300]!r}"
    assert message.endswith("… (truncated) (driver exit 1)") and len(message) < 1000, f"the cause was not bounded: {len(message)} chars"


def test_a_lost_job_with_no_marker_is_resubmitted_under_its_key_twice_and_then_fails(
    settings: MedallionSettings, dashboard: list[str], door: tuple[TestClient, _Bus]
) -> None:
    """D-6's budget. Each time the head loses the job and the destination holds no marker, the same key is submitted
    again (it creates a fresh job or re-attaches to a live one); after two, the run fails, bare, saying why."""
    _client, bus = door
    plan = _pass_one(settings, bus, {"token": "tok-1"})
    engine = _Engine(*([RunState.RUNNING, RunState.UNKNOWN] * 3))

    for _ in range(6):
        _sweep(settings, bus, engine)

    assert engine.submitted == [plan.action_id, plan.action_id]
    closed = _plan(settings, plan)
    assert closed.outcome is not None and closed.outcome.status == "failed"
    fail = next(event for event in bus.on(LINEAGE_TOPIC) if event["eventType"] == "FAIL")
    assert "facets" not in fail["outputs"][0], "a run that wrote nothing named a version"
    assert "vanished after 2 resubmit(s)" in fail["run"]["facets"]["errorMessage"]["message"]
