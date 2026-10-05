"""A producer door that acts on an EXISTING resource authorizes on THAT resource's project.

Owner ruling 2026-09-25, "Authorize on the resource". `/produce` names its WRITE TARGET in `?project=`,
so gating `can_administer` on the caller-chosen project is right there. A door that reads or stops
something that already exists has no such choice to offer: the tenant is a fact recorded on the
resource. Gating those doors on `?project=` let an admin of one project read or stop another's —
measured 2026-09-25 on e4e60b60 (the orchestrator's `probe_cross_tenant.py`): alice, admin of
`project:mine` only, got 200 on `GET /cascade/stalled?project=mine` listing acme's and other's cells.

Everything below runs the real routers and the real door. Faked: the OIDC verifier (the bearer's text
IS the sub), OpenFGA (a fixed grant set), the Ray dashboard behind both executors (an httpx transport), the lag
tick and the edge declaration it measures. A stage run and a training run are each a real plan in a plan store on
`tmp_path`. A service's token is real: the root
conftest's loopback issuer mints it and each door verifies it against that issuer. The producer reaches the
stage runner over a real ASGI hop carrying its own projected token, so the tenant the producer authorizes on
is the one the stage runner read off the run's plan.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientTuple

from medallion.api import cascade_lag_read, produce_auth, stage_ops, stage_runner_ops
from medallion.api import train as train_api
from medallion.core.config import MedallionSettings, get_settings
from medallion.services import ray_submit, stage_plans, train_plans
from medallion.services import train as train_service
from medallion.services.cascade_lag import AbsentEdgeMemo, LagTickReport, StalledTier
from medallion.services.trigger_guards import StageTrigger
from service_kit.exceptions import register_handlers
from service_kit.governed.audit import AUDIT_LOGGER, configure_audit
from service_kit.governed.machine_identity import ServiceAccountVerifier
from service_kit.lakehouse.commit_marker import CommitMarker
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.run_plans import PlanDocument, PlanKind
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp


APP_TOKEN = "the-estate-app-token"
CONFIGURED = "acme"
RUNNER = "silver-to-gold"
#: Where the planned runs read and write: a local path nothing is at, so resolving a stopped run reads its commit
#: marker as absent. A fake bucket would be a store that cannot answer, which the marker read refuses to read as absent.
_NOWHERE = "/nonexistent/rask-operator-doors"
#: alice administers `mine` only, bob the configured `acme` only, carol nothing; the ingest service's
#: subject administers `mine` only. `other` is a tenant none of them administers.
GRANTS = frozenset({("alice", "project:mine"), ("bob", "project:acme"), ("service-ingest", "project:mine")})

#: The service accounts, as `security-sa.yaml` names them, and the audience of the medallion's own doors.
NAMESPACE = "rask"
PRODUCER_SA = "rask-sa-medallion-producer"
INGEST_SA = "rask-sa-ingest"
#: The compute head's one account, which a stage runner's door also maps, for the outcome route alone.
HEAD_SA = "rask-sa-ray"
MEDALLION_AUDIENCE = "rask-medallion"


def _bearer(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {sub}"}


#: The app token daprd stamps on every invocation. It proves a sidecar delivered the request and names nobody.
SERVICE = {"dapr-api-token": APP_TOKEN}
#: What daprd stamps on a request the public gateway forwards for an anonymous caller.
PUBLIC = {**SERVICE, "dapr-caller-app-id": "gateway"}


class _Verifier:
    """The bearer's text is the sub, so a test names its caller in the header it sends."""

    def verify(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(sub=token)


def _stage(action_id: str, project: str | None) -> PlanDocument:
    """A stage run as the stage runner's dispatch plans it: the order names the tenant, the trigger rides whole."""
    order = WorkOrder(
        task="gold",
        source=WorkSource(uri=f"{_NOWHERE}/in", table_id="silver$features"),
        destination=WorkDestination(uri=f"{_NOWHERE}/out", table_id="gold$catalog"),
        stamp=WorkStamp(stage="gold", cardinality="1:1"),
        identity=WorkIdentity(run_id="run-1", project=project or ""),
        idempotency_key=action_id,
    )
    trigger = StageTrigger(token="tok-1", project=project).model_dump()
    return PlanDocument.for_order(
        order, kind=PlanKind.STAGE, engine="ray", command="python job.py", base_version=None, trigger=trigger, submitted_at=datetime.now(UTC)
    )


def _train(action_id: str, project: str) -> PlanDocument:
    """A training run as the producer's consumer plans it: the tenant on the plan, the trigger's pointers beside it."""
    return PlanDocument(
        action_id=action_id,
        run_id="run-1",
        kind=PlanKind.TRAIN,
        engine="ray",
        task="train",
        stage="models",
        from_uri="",
        to_uri=f"{_NOWHERE}/models/churn",
        to_id="models$churn",
        project=project,
        trigger={"token": "tok-1", "model": "churn"},
        submitted_at=datetime.now(UTC),
    )


#: The stage runs the stage runner hosts, by action id: the tenant each plan records (None, single-tenant).
STAGES: dict[str, str | None] = {"stage-mine": "mine", "stage-other": "other", "stage-single": None}
#: What the Ray dashboard was asked to stop, recorded by `ray_dashboard` and cleared per test.
_RAY_STOPS: list[str] = []
#: Jobs that had already ENDED when the dashboard was asked to stop them, with the state they ended in; every other
#: job is RUNNING. Cleared per test.
_RAY_ENDED: dict[str, str] = {}
#: The stage runner's identity: the owner its plans live under.
_RUNNER_IDENTITY = "service-silver-to-gold"
#: The training runs the producer planned, by action id: the tenant each plan records, or a document that does not
#: parse as a plan (``bytes``).
TRAINS: dict[str, str | bytes] = {
    "train-acme": CONFIGURED,
    "train-other": "other",
    "train-mine": "mine",
    "train-garbled": b"not json",
    "train-unsafe": "../acme",
    "train-listed": json.dumps([CONFIGURED]).encode(),
}
#: The producer's identity: the owner its training plans live under.
_PRODUCER_IDENTITY = "service-medallion-producer"


def _seed_trains(root: Path) -> dict[str, object]:
    """``TRAINS`` as plans under ``root``; answer the settings that point the producer at them."""
    update: dict[str, object] = {"control_root": str(root), "fga_service_identity": _PRODUCER_IDENTITY}
    store = train_plans.plan_store(MedallionSettings().model_copy(update=update))
    for action_id, project in TRAINS.items():
        if isinstance(project, bytes):
            document = root / "_plans" / _PRODUCER_IDENTITY / "runs" / f"{action_id}.json"
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_bytes(project)
        else:
            store.create(_train(action_id, project))
    return update


class _Fga:
    """OpenFGA over `GRANTS`, recording every round trip. Both doubles carry the real signatures."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.down = False

    async def check(
        self,
        _client: object,
        *,
        user: str,
        relation: str,
        obj: str,
        qualify: bool = True,
        contextual_tuples: list[ClientTuple] | None = None,
        context: dict[str, Any] | None = None,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        retry_max_backoff_seconds: float = 1.0,
    ) -> bool:
        self.calls.append(("check", [obj]))
        assert relation == "can_administer"
        return (user, obj) in GRANTS

    async def batch_check(
        self,
        _client: object,
        *,
        user: str,
        relation: str,
        objects: list[str],
        context: dict[str, Any] | None = None,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        retry_max_backoff_seconds: float = 1.0,
    ) -> dict[str, bool]:
        from lance_namespace import ServiceUnavailableError

        self.calls.append(("batch_check", list(objects)))
        assert relation == "can_administer"
        if self.down:
            raise ServiceUnavailableError("fga down")
        return {obj: (user, obj) in GRANTS for obj in objects}


@pytest.fixture
def fga(monkeypatch: pytest.MonkeyPatch) -> _Fga:
    double = _Fga()
    monkeypatch.setattr(produce_auth.fga, "check", double.check)
    monkeypatch.setattr(produce_auth.fga, "batch_check", double.batch_check)
    return double


#: The declared cells for three tenants; `acme` twice, so one batch must carry each project once.
EDGES = [("silver->gold", CONFIGURED), ("bronze->silver", CONFIGURED), ("silver->gold", "other"), ("bronze->silver", "mine")]


class _Ticks:
    """`run_lag_tick`, with its signature, reporting every edge it is handed as stalled and recording
    which edges each tick measured, so a filter applied after the measurement is visible as one."""

    def __init__(self) -> None:
        self.measured: list[list[tuple[str, str]]] = []

    def __call__(
        self, *, edges: Sequence[tuple[str, str]], published: object, consumed: object, gauge: object, memo: AbsentEdgeMemo | None = None
    ) -> LagTickReport:
        self.measured.append(list(edges))
        return LagTickReport(edges=len(edges), published_points=0, failed=0, unpublished_source=[StalledTier(edge=e, project=p) for e, p in edges])


class _Identities:
    """The two medallion doors' service accounts on the loopback issuer, as the chart maps them.

    The producer's door maps the ingest service's account and a stage runner's door maps the producer's
    alone. The producer's own token sits in the file the kubelet would project.
    """

    def __init__(self, issuer: Any, directory: Any) -> None:
        self._issuer = issuer
        self.token_file = directory / "rask-medallion-token"
        self.token_file.write_text(self._mint(PRODUCER_SA))

    def _mint(self, sa: str) -> str:
        return self._issuer.mint(sa, audience=MEDALLION_AUDIENCE, namespace=NAMESPACE)

    def verifier(self, sa: str, subject: str) -> ServiceAccountVerifier:
        """A door's verifier mapping ``sa`` alone."""
        return _sa_verifier(self._issuer, {_account(sa): subject})

    def producer_bearer(self) -> dict[str, str]:
        return _bearer(self._mint(PRODUCER_SA))

    def service_bearer(self) -> dict[str, str]:
        """The ingest service's token, whose subject administers `mine` only."""
        return _bearer(self._mint(INGEST_SA))


@pytest.fixture
def identities(sa_issuer: Any, tmp_path: Any) -> _Identities:
    return _Identities(sa_issuer, tmp_path)


@pytest.fixture(autouse=True)
def ray_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard the real Ray adapter reads a run's state from and stops a job through."""
    _RAY_STOPS.clear()
    _RAY_ENDED.clear()

    def handle(request: httpx.Request) -> httpx.Response:
        job = request.url.path.split("/")[3]
        if request.method == "POST" and request.url.path.endswith("/stop"):
            _RAY_STOPS.append(job)
            # Ray answers the stop of a job that already ended with `stopped: false`.
            return httpx.Response(200, json={"stopped": job not in _RAY_ENDED})
        return httpx.Response(200, json={"status": _RAY_ENDED.get(job, "RUNNING")})

    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)


def _stage_runner(stages: dict[str, str | bytes | None], root: Path, identities: _Identities | None = None) -> FastAPI:
    """A stage runner hosting ``stages`` as plans under ``root``; a ``bytes`` value is a plan document that does not parse."""
    settings = MedallionSettings().model_copy(update={"control_root": str(root), "fga_service_identity": _RUNNER_IDENTITY})
    store = stage_plans.plan_store(settings)
    for action_id, project in stages.items():
        if isinstance(project, bytes):
            document = root / "_plans" / _RUNNER_IDENTITY / "runs" / f"{action_id}.json"
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_bytes(project)
        else:
            store.create(_stage(action_id, project))
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(stage_ops.router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.dapr = _Bus()
    app.state.stopped = _RAY_STOPS
    if identities is not None:
        app.state.sa_oidc = identities.verifier(PRODUCER_SA, "service-medallion-producer")
    return app


class _Bus:
    """The stage runner's Dapr client, recording what a resolved run published: a hand-off, or a FAIL."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str = "application/json", publish_metadata: dict[str, str] | None = None
    ) -> None:
        self.published.append((topic_name, json.loads(data)))


#: A wired authorization client; the FGA double ignores it, so `None` is the one value that differs.
_WIRED = object()


def _producer(
    stage_runner: Any, identities: _Identities | None = None, *, fga_client: object | None = _WIRED, settings_update: dict[str, object] | None = None
) -> FastAPI:
    """The producer's operator routers, assembled in the order `build_lance_service_app` uses."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(stage_runner_ops.router)
    app.include_router(cascade_lag_read.router)
    app.include_router(train_api.router)
    settings = MedallionSettings().model_copy(
        update={
            "oidc_enabled": True,
            "produce_admin_project": CONFIGURED,
            "stage_runner_urls": {RUNNER: "http://sr:8000"},
            **({"medallion_identity_token_file": str(identities.token_file)} if identities is not None else {}),
            **(settings_update or {}),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.oidc = _Verifier()
    if identities is not None:
        app.state.sa_oidc = identities.verifier(INGEST_SA, "service-ingest")
    app.state.fga = fga_client
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=stage_runner))
    app.state.dapr = _Bus()
    app.state.stopped = _RAY_STOPS
    return app


@pytest.fixture
def stage_runner(identities: _Identities, tmp_path: Path) -> FastAPI:
    return _stage_runner(dict(STAGES), tmp_path / "control", identities)


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> _Ticks:
    """The tick and the declaration it measures. The door imports its readers lazily, so the
    declaration is replaced at its own module."""
    double = _Ticks()
    monkeypatch.setattr(cascade_lag_read, "run_lag_tick", double)
    monkeypatch.setattr("medallion.services.cascade_lag_readers.declared_edges", lambda _settings: list(EDGES))
    return double


@pytest.fixture
def trains(tmp_path: Path) -> dict[str, object]:
    return _seed_trains(tmp_path / "producer-control")


@pytest.fixture
def producer(stage_runner: FastAPI, identities: _Identities, fga: _Fga, ticks: _Ticks, trains: dict[str, object]) -> Iterator[TestClient]:
    with TestClient(_producer(stage_runner, identities, settings_update=trains), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def unwired(stage_runner: FastAPI, identities: _Identities, fga: _Fga, ticks: _Ticks, trains: dict[str, object]) -> Iterator[TestClient]:
    """The producer with OIDC on and no authorization client: a person must never be let through."""
    with TestClient(_producer(stage_runner, identities, fga_client=None, settings_update=trains), raise_server_exceptions=False) as client:
        yield client


def _terminated(app: FastAPI) -> list[str]:
    """What the app stopped through the Ray dashboard: a stage runner's stage jobs or the producer's training jobs."""
    return app.state.stopped


def _show(stage: str) -> str:
    return f"/stage-runners/{RUNNER}/stages/{stage}"


def _stop(stage: str) -> str:
    return f"/stage-runners/{RUNNER}/stages/{stage}/terminate"


# ── the stage runner names the tenant of the instance it hosts ──────────────────────────────────────


def test_the_stage_runner_reports_the_project_its_instance_RECORDS(stage_runner: FastAPI, identities: _Identities) -> None:
    """The producer cannot read another app's plans, so the tenant has to cross this hop."""
    with TestClient(stage_runner) as client:
        assert client.get("/stages/stage-other", headers=identities.producer_bearer()).json()["project"] == "other"
        single = client.get("/stages/stage-single", headers=identities.producer_bearer()).json()["project"]
        assert single == "", "a single-tenant run must be told apart from an unreadable one"


def test_an_UNREADABLE_instance_names_no_project(identities: _Identities, tmp_path: Path) -> None:
    with TestClient(_stage_runner({"stage-garbled": b"not json"}, tmp_path, identities)) as client:
        body = client.get("/stages/stage-garbled", headers=identities.producer_bearer()).json()

    assert body["status"] == "UNREADABLE", "the status question is still answered"
    assert body["project"] is None


# ── stage show / terminate ──────────────────────────────────────────────────────────────────────────


def test_an_admin_sees_THEIR_OWN_projects_stage_without_naming_it(producer: TestClient) -> None:
    response = producer.get(_show("stage-mine"), headers=_bearer("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["project"] == "mine"


@pytest.mark.parametrize("query", ["?project=mine"])
def test_an_admin_of_one_project_is_REFUSED_anothers_stage(producer: TestClient, query: str) -> None:
    """`?project=mine` is the measured lever: it moved the gate onto a project alice administers."""
    response = producer.get(_show("stage-other") + query, headers=_bearer("alice"))

    assert response.status_code == 403, response.text
    assert "project:other" in response.text, response.text


@pytest.mark.parametrize("query", ["?project=mine"])
def test_an_admin_of_one_project_CANNOT_STOP_anothers_stage(producer: TestClient, stage_runner: FastAPI, query: str) -> None:
    response = producer.post(_stop("stage-other") + query, headers=_bearer("alice"))

    assert response.status_code == 403, response.text
    assert _terminated(stage_runner) == [], "the refusal must come BEFORE the terminate is forwarded"


def test_an_admin_CAN_stop_their_own_projects_stage(producer: TestClient, stage_runner: FastAPI) -> None:
    """The job had already SUCCEEDED when the stop reached it (its report lost, the sweep's tick not yet come), so the
    run closes as the engine says it ended: succeeded, with its next tier woken, never a FAIL (D-4)."""
    _RAY_ENDED["stage-mine"] = "SUCCEEDED"

    response = producer.post(_stop("stage-mine"), headers=_bearer("alice"))

    assert response.status_code == 202, response.text
    assert _terminated(stage_runner) == ["stage-mine"]
    assert response.json()["status"] == "succeeded", f"a job Ray says SUCCEEDED was closed {response.json()['status']!r} by the stop"
    published = stage_runner.state.dapr.published
    assert [payload.get("ray_submission_id") for _topic, payload in published if payload.get("ray_job_done")] == ["stage-mine"], (
        f"the succeeded run's next tier was not woken: {published}"
    )


def test_a_SINGLE_TENANT_stage_belongs_to_the_configured_project(producer: TestClient) -> None:
    """A trigger with no project is the single-tenant cascade `/produce` starts with none — gated on
    the configured project there, so on the same project here."""
    assert producer.get(_show("stage-single"), headers=_bearer("bob")).status_code == 200
    assert producer.get(_show("stage-single"), headers=_bearer("alice")).status_code == 403


def test_an_UNKNOWN_stage_is_still_404(producer: TestClient) -> None:
    assert producer.get(_show("stage-nope"), headers=_bearer("alice")).status_code == 404


@pytest.mark.parametrize("who", ["service", "person"])
def test_an_UNKNOWN_stage_runner_is_NAMED_to_every_admitted_caller(producer: TestClient, identities: _Identities, who: str) -> None:
    """The typo against a values-driven list is the common cause, and the configured names are
    `GET /stage-runners`' to give any admitted caller, carol included."""
    headers = identities.service_bearer() if who == "service" else _bearer("carol")
    response = producer.get("/stage-runners/typo/stages/stage-mine", headers=headers)

    assert response.status_code == 404, response.text
    assert RUNNER in response.text, response.text


def _runner_without_the_field() -> FastAPI:
    """A stage runner build whose status body carries no `project` key at all — a mixed rollout."""
    app = FastAPI()
    app.state.stopped = []

    @app.get("/stages/{instance_id}")
    async def show(instance_id: str) -> dict[str, object]:
        return {"instance_id": instance_id, "status": "RUNNING", "submission_id": None, "polls_done": 0}

    @app.post("/stages/{instance_id}/terminate", status_code=202)
    async def stop(instance_id: str) -> dict[str, str]:
        app.state.stopped.append(instance_id)
        return {"instance_id": instance_id, "detail": "stopped"}

    return app


@pytest.mark.parametrize(
    "runner",
    [lambda ids, root: _stage_runner({"stage-x": b"not json"}, root, ids), lambda _ids, _root: _runner_without_the_field()],
    ids=["unreadable-input", "older-build"],
)
def test_a_stage_whose_tenant_cannot_be_read_is_REFUSED_to_a_person(fga: _Fga, identities: _Identities, runner: Any, tmp_path: Path) -> None:
    """Nothing to authorize on. Reading it as single-tenant would hand a tenant's run to acme's admins."""
    app = runner(identities, tmp_path)
    with TestClient(_producer(app, identities), raise_server_exceptions=False) as client:
        refused = client.post(_stop("stage-x") + "?project=acme", headers=_bearer("bob"))

    assert refused.status_code == 503, refused.text
    assert _terminated(app) == []


def test_an_UNWIRED_authorization_service_refuses_a_person_THEIR_OWN_stage(unwired: TestClient, stage_runner: FastAPI) -> None:
    """alice administers `mine`, so only the missing client can refuse her, and it must, as 503."""
    shown = unwired.get(_show("stage-mine"), headers=_bearer("alice"))
    stopped = unwired.post(_stop("stage-mine"), headers=_bearer("alice"))

    assert (shown.status_code, stopped.status_code) == (503, 503), (shown.text, stopped.text)
    assert _terminated(stage_runner) == []


def _runner_reporting(project: object) -> FastAPI:
    """A stage runner whose status names ``project`` verbatim."""
    app = FastAPI()
    app.state.stopped = []

    @app.get("/stages/{instance_id}")
    async def show(instance_id: str) -> dict[str, object]:
        return {"instance_id": instance_id, "status": "RUNNING", "submission_id": None, "polls_done": 0, "project": project}

    @app.post("/stages/{instance_id}/terminate", status_code=202)
    async def stop(instance_id: str) -> dict[str, str]:
        app.state.stopped.append(instance_id)
        return {"instance_id": instance_id, "detail": "stopped"}

    return app


def test_a_stage_whose_recorded_project_is_UNSAFE_is_refused_to_a_person(fga: _Fga, identities: _Identities) -> None:
    """A project id no mint rule issues is nothing to authorize on, and never an FGA object."""
    app = _runner_reporting("../acme")
    with TestClient(_producer(app, identities), raise_server_exceptions=False) as client:
        refused = client.post(_stop("stage-x"), headers=_bearer("bob"))

    assert refused.status_code == 503, refused.text
    assert _terminated(app) == []
    assert fga.calls == []


# ── a service is held to its own tenant ─────────────────────────────────────────────────────────────


def _account(sa: str) -> str:
    return f"system:serviceaccount:{NAMESPACE}:{sa}"


def _sa_verifier(issuer: Any, subjects: dict[str, str]) -> ServiceAccountVerifier:
    """A door's verifier against the loopback issuer, with the fetch credential and CA the cluster demands."""
    return ServiceAccountVerifier(
        issuer.issuer, MEDALLION_AUDIENCE, subjects, cache_ttl=60, leeway=60, fetch_token_file=str(issuer.fetch_token_file), ca_file=str(issuer.ca_file)
    )


@pytest.fixture
def held(sa_issuer: Any, fga: _Fga, ticks: _Ticks, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, TestClient, FastAPI]]:
    """The producer admitting the ingest service, and a stage runner whose door maps the producer and the compute head.

    Each door maps only the accounts the chart names for it, and the producer forwards with its own
    projected `rask-medallion` token, read from the file the kubelet would project.
    """
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    runner = _stage_runner(dict(STAGES), tmp_path / "control")
    runner.state.sa_oidc = _sa_verifier(sa_issuer, {_account(PRODUCER_SA): "service-medallion-producer", _account(HEAD_SA): "service-trainer"})
    token_file = tmp_path / "rask-medallion-token"
    token_file.write_text(sa_issuer.mint(PRODUCER_SA, audience=MEDALLION_AUDIENCE, namespace=NAMESPACE))
    producer = _producer(runner, settings_update={"medallion_identity_token_file": str(token_file)})
    producer.state.sa_oidc = _sa_verifier(sa_issuer, {_account(INGEST_SA): "service-ingest"})
    with TestClient(producer, raise_server_exceptions=False) as producer_client, TestClient(runner, raise_server_exceptions=False) as runner_client:
        yield producer_client, runner_client, runner


@pytest.mark.parametrize(
    ("door", "caller", "path", "status", "terminated"),
    [
        pytest.param("producer", INGEST_SA, _stop("stage-mine"), 202, ["stage-mine"], id="producer-stops-a-run-of-a-project-it-administers"),
        pytest.param("producer", INGEST_SA, _stop("stage-other"), 403, [], id="producer-refuses-a-run-of-another-project"),
        pytest.param("stage-runner", INGEST_SA, "/stages/stage-other/terminate", 401, [], id="stage-runner-refuses-a-caller-that-is-not-the-producer"),
        pytest.param("stage-runner", HEAD_SA, "/stages/stage-other/terminate", 403, [], id="stage-runner-refuses-the-compute-head-it-maps-for-outcomes"),
    ],
)
def test_a_service_token_stops_only_its_own_tenants_stage(
    held: tuple[TestClient, TestClient, FastAPI], sa_issuer: Any, door: str, caller: str, path: str, status: int, terminated: list[str]
) -> None:
    """A service is authorized as the subject its account maps to, exactly like a person ([[LH-220]] clause 5).

    The request carries what a service invocation carries: the app token daprd stamps, which names
    nobody, beside the service's own projected token. The ingest service's subject administers `mine`
    only. Going round the producer to the stage runner's ClusterIP does not escape the check, because
    the stage runner's operator routes admit the producer's account and no other: the compute head, which
    its door maps for the outcome route, verifies and is refused here.
    """
    producer, runner_client, runner = held
    client = producer if door == "producer" else runner_client
    headers = {**SERVICE, **_bearer(sa_issuer.mint(caller, audience=MEDALLION_AUDIENCE, namespace=NAMESPACE))}

    response = client.post(path, headers=headers)

    assert response.status_code == status, response.text
    assert _terminated(runner) == terminated


# ── the deployment's stage-runner list ──────────────────────────────────────────────────────────────


def test_a_signed_in_caller_administering_NOTHING_reads_the_runner_list(producer: TestClient) -> None:
    """Owner default 2026-09-26: the names are deployment config, the same for every tenant."""
    response = producer.get("/stage-runners", headers=_bearer("carol"))

    assert response.status_code == 200, response.text
    assert response.json() == {"stage_runners": [RUNNER]}


@pytest.mark.parametrize("headers", [PUBLIC, {}], ids=["public-front-door", "no-credential"])
def test_the_runner_list_is_REFUSED_to_a_caller_nobody_signed_in(producer: TestClient, headers: dict[str, str]) -> None:
    assert producer.get("/stage-runners", headers=headers).status_code == 403


@pytest.mark.parametrize("query", ["?project=x"])
def test_a_PROJECT_does_not_change_the_runner_list(producer: TestClient, query: str) -> None:
    """The answer names no tenant, so there is no project to gate on: `x`, an id no mint issues, is
    ignored like the rest."""
    plain = producer.get("/stage-runners", headers=_bearer("alice"))
    asked = producer.get("/stage-runners" + query, headers=_bearer("alice"))

    assert (asked.status_code, asked.json()) == (plain.status_code, plain.json()) == (200, {"stage_runners": [RUNNER]})


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("who", ["service", "admin-of-mine", "public", "none"])
def test_a_stage_door_tells_no_caller_a_runner_name_the_list_withholds(fga: _Fga, identities: _Identities, who: str) -> None:
    """A configured runner that is down answers 502 before any run is read, where an unknown name
    answers 404, so every caller a stage door admits can tell a real runner from a made-up one. The
    list must answer exactly those callers, or the doors are an oracle for what it withholds."""
    headers = {"service": identities.service_bearer(), "admin-of-mine": _bearer("alice"), "public": PUBLIC, "none": {}}[who]
    app = _producer(FastAPI(), identities)
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_refused))
    with TestClient(app, raise_server_exceptions=False) as client:
        door = client.get(_show("stage-mine"), headers=headers)
        listed = client.get("/stage-runners", headers=headers)

    assert door.status_code in {502, 403}, door.text
    assert (door.status_code == 502) == (listed.status_code == 200), (door.text, listed.text)


# ── the stalled-tier read ───────────────────────────────────────────────────────────────────────────


def _projects(response: httpx.Response) -> list[str]:
    return [cell["project"] for cell in response.json()["unpublished_source"]]


@pytest.mark.parametrize("query", ["?project=mine"])
def test_an_admin_sees_ONLY_the_stalled_cells_of_projects_they_administer(producer: TestClient, query: str) -> None:
    response = producer.get("/cascade/stalled" + query, headers=_bearer("alice"))

    assert response.status_code == 200, response.text
    assert _projects(response) == ["mine"]


def test_a_caller_administering_NONE_gets_an_empty_answer(producer: TestClient) -> None:
    """Empty rather than 403, like ingest's cross-tenant listing: a 403 for the whole call says that
    somebody's cascade has stalled."""
    response = producer.get("/cascade/stalled", headers=_bearer("carol"))

    assert response.status_code == 200, response.text
    assert _projects(response) == []


def test_an_authz_OUTAGE_is_503_never_an_empty_answer(producer: TestClient, fga: _Fga) -> None:
    fga.down = True

    assert producer.get("/cascade/stalled", headers=_bearer("alice")).status_code == 503


def test_an_UNWIRED_authorization_service_is_503_and_measures_nothing(unwired: TestClient, ticks: _Ticks) -> None:
    assert unwired.get("/cascade/stalled", headers=_bearer("alice")).status_code == 503
    assert ticks.measured == []


def test_the_SINGLE_TENANT_row_belongs_to_the_configured_project(producer: TestClient, fga: _Fga, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry with no tenants, or one that cannot be read, declares the unqualified lanes as project
    `""`: the tables `/produce` writes with no `?project=`, gated there on the configured project.
    `project:` names no object at all."""
    monkeypatch.setattr("medallion.services.cascade_lag_readers.declared_edges", lambda _settings: [("silver->gold", "")])

    assert _projects(producer.get("/cascade/stalled", headers=_bearer("bob"))) == [""]
    assert _projects(producer.get("/cascade/stalled", headers=_bearer("alice"))) == []
    assert fga.calls == [("batch_check", ["project:acme"]), ("batch_check", ["project:acme"])]


# ── the training run ──────────────────────────────────────────────────────────────────────────────


def test_a_training_run_is_gated_on_the_project_it_RECORDS(producer: TestClient) -> None:
    """The recorded project decides, not the deployment's: a run naming `other` is refused to acme's
    admin, and one naming `mine` is shown to mine's."""
    assert producer.get("/trains/train-other", headers=_bearer("bob")).status_code == 403
    assert producer.get("/trains/train-mine", headers=_bearer("alice")).status_code == 200


def test_a_training_run_of_another_project_CANNOT_BE_STOPPED(producer: TestClient) -> None:
    response = producer.post("/trains/train-other/terminate", headers=_bearer("bob"))

    assert response.status_code == 403, response.text
    assert _terminated(cast("FastAPI", producer.app)) == []


def test_an_UNWIRED_authorization_service_refuses_a_person_THEIR_OWN_training_run(unwired: TestClient) -> None:
    assert unwired.get("/trains/train-acme", headers=_bearer("bob")).status_code == 503
    assert unwired.post("/trains/train-acme/terminate", headers=_bearer("bob")).status_code == 503
    assert _terminated(cast("FastAPI", unwired.app)) == []


def test_a_training_run_whose_plan_cannot_be_read_is_REFUSED_to_a_person(producer: TestClient) -> None:
    """Unreadable is not the configured project: reading it so would hand an unknown run to acme's admins."""
    assert producer.get("/trains/train-garbled", headers=_bearer("bob")).status_code == 503


@pytest.mark.parametrize("instance_id", ["train-unsafe", "train-listed"])
def test_a_training_run_recording_no_USABLE_project_is_refused_to_a_person(producer: TestClient, fga: _Fga, instance_id: str) -> None:
    """An unsafe project id, or a plan that is not a mapping, names no project to authorize on: never
    an FGA object, and never read as the configured project."""
    response = producer.get(f"/trains/{instance_id}", headers=_bearer("bob"))

    assert response.status_code == 503, response.text
    assert fga.calls == []


def test_a_training_stop_is_logged_with_WHO_asked(producer: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=train_api.log.name):
        assert producer.post("/trains/train-acme/terminate", headers=_bearer("bob")).status_code == 202

    [line] = [r for r in caplog.records if r.getMessage() == "medallion_train_run_termination_requested"]
    assert (line.__dict__["instance_id"], line.__dict__["subject"]) == ("train-acme", "bob")


def test_the_training_doors_serve_the_run_the_consumer_PLANS_on_the_configured_project(
    stage_runner: FastAPI, identities: _Identities, fga: _Fga, tmp_path: Path
) -> None:
    """The id and the tenant come from the training consumer, not from a literal: it plans every run under the
    deployment's project whatever the trigger claims, and the doors serve that plan to that project's admin alone.
    A consumer whose ids the doors refused would leave every real training run unobservable and unstoppable."""
    root = tmp_path / "planned"
    update: dict[str, object] = {
        "control_root": str(root / "control"),
        "fga_service_identity": _PRODUCER_IDENTITY,
        "ray_enabled": True,
        "s3_endpoint": "http://rustfs:9000",
        "bronze_uri": str(root / "medallion" / "bronze"),
    }
    app = _producer(stage_runner, identities, settings_update=update)
    settings = app.dependency_overrides[get_settings]()
    trigger = {"token": "tok-1", "model": "churn", "features": [{"dataset": "silver$features", "version": 7}], "config": {}, "project": "other"}
    assert asyncio.run(train_service.handle_train_trigger(settings, {"data": trigger}, dapr=_Bus())) == {"status": "SUCCESS"}
    ((minted, _written),) = train_plans.plan_store(settings).open_entries()
    # The job had already published its model, its commit carrying the run's marker, when the stop reached it: Ray
    # answers the stop with `stopped: false` and SUCCEEDED, and the run must close succeeded, never FAIL (D-4).
    planned = asyncio.run(train_plans.read(settings, minted))
    assert planned is not None
    lance.write_dataset(pa.table({"version": [1]}), planned.to_uri, transaction_properties=CommitMarker(action_id=minted).properties())
    _RAY_ENDED[minted] = "SUCCEEDED"

    with TestClient(app, raise_server_exceptions=False) as client:
        refused = client.get(f"/trains/{minted}", headers=_bearer("alice"))
        shown = client.get(f"/trains/{minted}", headers=_bearer("bob"))
        stopped = client.post(f"/trains/{minted}/terminate", headers=_bearer("bob"))
        after = client.get(f"/trains/{minted}", headers=_bearer("bob"))

    assert (refused.status_code, shown.status_code, stopped.status_code) == (403, 200, 202), (refused.text, shown.text, stopped.text)
    assert _terminated(app) == [minted]
    assert after.json()["status"] == "SUCCEEDED", f"a training job that published its model was closed {after.json()['status']!r} by the stop"


# ── the decision is audited with the RESOURCE's project ─────────────────────────────────────────────


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audited() -> Iterator[list[logging.LogRecord]]:
    handler = _Capture()
    logger = logging.getLogger(AUDIT_LOGGER)
    configure_audit(enabled=True)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        configure_audit(enabled=True)


def _all_decisions(records: list[logging.LogRecord]) -> list[tuple[object, ...]]:
    fields = [{k: v for k, v in r.__dict__.items() if k.startswith("audit.")} for r in records]
    return [(f["audit.action"], f["audit.outcome"], f["audit.subject"], f["audit.resource"], f.get("audit.reason")) for f in fields if "audit.action" in f]


def test_a_SERVICE_read_of_a_training_run_is_audited_on_the_RUNS_project(
    producer: TestClient, identities: _Identities, audited: list[logging.LogRecord]
) -> None:
    """Recorded as a person's is: the decision on the watch's project, under the subject the account maps to."""
    producer.get("/trains/train-other", headers=identities.service_bearer())

    assert _all_decisions(audited) == [("can_administer", "deny", "service-ingest", "project:other", None)]


def test_a_runner_list_read_is_AUDITED_as_the_admission_it_is(producer: TestClient, identities: _Identities, audited: list[logging.LogRecord]) -> None:
    """No project is checked, so the record is the admission, as the catalog records an authentication-only
    read: a service's under the subject its account maps to, against the path the door's refusal names."""
    assert producer.get("/stage-runners", headers=identities.service_bearer()).status_code == 200

    assert _all_decisions(audited) == [("authn", "success", "service-ingest", "/stage-runners", None)]


def test_a_PUBLIC_callers_refusal_names_what_it_asked_for(
    producer: TestClient, stage_runner: FastAPI, identities: _Identities, audited: list[logging.LogRecord]
) -> None:
    """A service never arrives through the public front door, so one that does is refused at the door,
    before any run is read, and the record names the request's target rather than the configured project."""
    response = producer.post(_stop("stage-other"), headers={**PUBLIC, **identities.service_bearer()})

    assert response.status_code == 403, response.text
    assert _terminated(stage_runner) == []
    assert _all_decisions(audited) == [("authn", "deny", "service-ingest", _stop("stage-other"), "public_caller")]


# ── which doors may take `?project=` at all ─────────────────────────────────────────────────────────

#: The producer operations whose `?project=` names what the call WRITES. Every other door acts on
#: something that already exists and reads the tenant off it, or reads deployment config no tenant owns,
#: so a new door that declares the parameter fails here until someone decides which kind it is.
_PROJECT_PARAM_ALLOWED = {
    ("post", "/produce"): "the write target: the cascade head seeds THIS project's bronze",
}


def _declares_project(operation: dict[str, Any]) -> bool:
    return any(p.get("in") == "query" and p.get("name") == "project" for p in operation.get("parameters", []))


def test_only_WRITE_TARGET_doors_take_a_caller_chosen_project() -> None:
    from medallion.producer import app

    declared = {(method, path) for path, item in app.openapi()["paths"].items() for method, op in item.items() if _declares_project(op)}

    assert declared == set(_PROJECT_PARAM_ALLOWED), f"doors taking ?project= beyond the classified set: {sorted(declared - set(_PROJECT_PARAM_ALLOWED))}"
