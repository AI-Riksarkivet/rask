"""A producer door that acts on an EXISTING resource authorizes on THAT resource's project.

Owner ruling 2026-09-25, "Authorize on the resource". `/produce` names its WRITE TARGET in `?project=`,
so gating `can_administer` on the caller-chosen project is right there. A door that reads or stops
something that already exists has no such choice to offer: the tenant is a fact recorded on the
resource. Gating those doors on `?project=` let an admin of one project read or stop another's —
measured 2026-09-25 on e4e60b60 (the orchestrator's `probe_cross_tenant.py`): alice, admin of
`project:mine` only, got 200 on `GET /cascade/stalled?project=mine` listing acme's and other's cells.

Everything below runs the real routers and the real door. Faked: the OIDC verifier (the bearer's text
IS the sub), OpenFGA (a fixed grant set), the stage runner's workflow engine, the lag tick and the
edge declaration it measures. The producer reaches the stage runner over a real ASGI hop carrying the
real service token, so the tenant the producer authorizes on is the one the stage runner read off the
instance it hosts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Sequence
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientTuple

from medallion.api import cascade_lag_read, produce_auth, stage_ops, stage_runner_ops
from medallion.api import train as train_api
from medallion.core.config import MedallionSettings, get_settings
from medallion.services import train as train_service
from medallion.services.cascade_lag import AbsentEdgeMemo, LagTickReport, StalledTier
from medallion.services.trigger_guards import StageTrigger
from medallion.workflow import StageJobSpec, TrainJobSpec
from service_kit.exceptions import register_handlers
from service_kit.governed.audit import AUDIT_LOGGER, configure_audit
from service_kit.lakehouse.ns_errors import install_problem_handlers


APP_TOKEN = "the-estate-app-token"
CONFIGURED = "acme"
RUNNER = "silver-to-gold"
#: alice administers `mine` only, bob the configured `acme` only, carol nothing. `other` is a tenant
#: none of them administers.
GRANTS = frozenset({("alice", "project:mine"), ("bob", "project:acme")})


def _bearer(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {sub}"}


SERVICE = {"dapr-api-token": APP_TOKEN}


class _Verifier:
    """The bearer's text is the sub, so a test names its caller in the header it sends."""

    def verify(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(sub=token)


class _State:
    """`WorkflowState`'s fields these routes read; ``name`` is the registered workflow the SDK proxies."""

    def __init__(self, payload: dict[str, Any] | str, *, name: str) -> None:
        self.serialized_input = payload if isinstance(payload, str) else json.dumps(payload)
        self.runtime_status = WorkflowStatus.RUNNING
        self.name = name


class _Workflows:
    """`DaprWorkflowClient`'s two calls these routes make, with the SDK's own signatures."""

    def __init__(self, instances: dict[str, _State]) -> None:
        self._instances = instances
        self.terminated: list[str] = []

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._instances.get(instance_id)

    def terminate_workflow(self, instance_id: str, *, output: Any | None = None, recursive: bool = True) -> None:
        self.terminated.append(instance_id)


def _stage(project: str | None) -> _State:
    """A stage instance as `_dispatch_stage_workflow` persists it: the trigger rides the spec whole."""
    trigger = StageTrigger(token="tok-1", project=project).model_dump()
    return _State(StageJobSpec(from_uri="s3://wh/in", to_uri="s3://wh/out", stage="gold", trigger=trigger).model_dump(), name="stage_run")


def _train(project: str) -> _State:
    return _State(TrainJobSpec(token="tok-1", model="churn", submission_id="ray-train-tok-1", project=project).model_dump(), name="train_run")


STAGES = {"stage-mine": _stage("mine"), "stage-other": _stage("other"), "stage-single": _stage(None)}
TRAINS = {
    "train-acme": _train(CONFIGURED),
    "train-other": _train("other"),
    "train-mine": _train("mine"),
    "train-garbled": _State("not json", name="train_run"),
}


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


def _stage_runner(stages: dict[str, _State]) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(stage_ops.router)
    app.state.workflow_client = _Workflows(stages)
    return app


#: A wired authorization client; the FGA double ignores it, so `None` is the one value that differs.
_WIRED = object()


def _producer(stage_runner: Any, *, fga_client: object | None = _WIRED) -> FastAPI:
    """The producer's operator routers, assembled in the order `build_lance_service_app` uses."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(stage_runner_ops.router)
    app.include_router(cascade_lag_read.router)
    app.include_router(train_api.router)
    settings = MedallionSettings().model_copy(
        update={"oidc_enabled": True, "produce_admin_project": CONFIGURED, "app_api_token": APP_TOKEN, "stage_runner_urls": {RUNNER: "http://sr:8000"}}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.oidc = _Verifier()
    app.state.fga = fga_client
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=stage_runner))
    app.state.workflow_client = _Workflows(dict(TRAINS))
    return app


@pytest.fixture
def stage_runner(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    return _stage_runner(dict(STAGES))


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> _Ticks:
    """The tick and the declaration it measures. The door imports its readers lazily, so the
    declaration is replaced at its own module."""
    double = _Ticks()
    monkeypatch.setattr(cascade_lag_read, "run_lag_tick", double)
    monkeypatch.setattr("medallion.services.cascade_lag_readers.declared_edges", lambda _settings: list(EDGES))
    return double


@pytest.fixture
def producer(stage_runner: FastAPI, fga: _Fga, ticks: _Ticks) -> Iterator[TestClient]:
    with TestClient(_producer(stage_runner), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def unwired(stage_runner: FastAPI, fga: _Fga, ticks: _Ticks) -> Iterator[TestClient]:
    """The producer with OIDC on and no authorization client: a person must never be let through."""
    with TestClient(_producer(stage_runner, fga_client=None), raise_server_exceptions=False) as client:
        yield client


def _terminated(app: FastAPI) -> list[str]:
    return app.state.workflow_client.terminated


def _show(stage: str) -> str:
    return f"/stage-runners/{RUNNER}/stages/{stage}"


def _stop(stage: str) -> str:
    return f"/stage-runners/{RUNNER}/stages/{stage}/terminate"


# ── the stage runner names the tenant of the instance it hosts ──────────────────────────────────────


def test_the_stage_runner_reports_the_project_its_instance_RECORDS(stage_runner: FastAPI) -> None:
    """The producer cannot read another app's workflow state, so the tenant has to cross this hop."""
    with TestClient(stage_runner) as client:
        assert client.get("/stages/stage-other", headers=SERVICE).json()["project"] == "other"
        assert client.get("/stages/stage-single", headers=SERVICE).json()["project"] == "", "a single-tenant run must be told apart from an unreadable one"


def test_an_UNREADABLE_instance_names_no_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    with TestClient(_stage_runner({"stage-garbled": _State("not json", name="stage_run")})) as client:
        body = client.get("/stages/stage-garbled", headers=SERVICE).json()

    assert body["status"] == "RUNNING", "the status question is still answered"
    assert body["project"] is None


# ── stage show / terminate ──────────────────────────────────────────────────────────────────────────


def test_an_admin_sees_THEIR_OWN_projects_stage_without_naming_it(producer: TestClient) -> None:
    response = producer.get(_show("stage-mine"), headers=_bearer("alice"))

    assert response.status_code == 200, response.text
    assert response.json()["project"] == "mine"


@pytest.mark.parametrize("query", ["", "?project=mine"])
def test_an_admin_of_one_project_is_REFUSED_anothers_stage(producer: TestClient, query: str) -> None:
    """`?project=mine` is the measured lever: it moved the gate onto a project alice administers."""
    response = producer.get(_show("stage-other") + query, headers=_bearer("alice"))

    assert response.status_code == 403, response.text
    assert "project:other" in response.text, response.text


@pytest.mark.parametrize("query", ["", "?project=mine"])
def test_an_admin_of_one_project_CANNOT_STOP_anothers_stage(producer: TestClient, stage_runner: FastAPI, query: str) -> None:
    response = producer.post(_stop("stage-other") + query, headers=_bearer("alice"))

    assert response.status_code == 403, response.text
    assert _terminated(stage_runner) == [], "the refusal must come BEFORE the terminate is forwarded"


def test_an_admin_CAN_stop_their_own_projects_stage(producer: TestClient, stage_runner: FastAPI) -> None:
    response = producer.post(_stop("stage-mine"), headers=_bearer("alice"))

    assert response.status_code == 202, response.text
    assert _terminated(stage_runner) == ["stage-mine"]


def test_a_SINGLE_TENANT_stage_belongs_to_the_configured_project(producer: TestClient) -> None:
    """A trigger with no project is the single-tenant cascade `/produce` starts with none — gated on
    the configured project there, so on the same project here."""
    assert producer.get(_show("stage-single"), headers=_bearer("bob")).status_code == 200
    assert producer.get(_show("stage-single"), headers=_bearer("alice")).status_code == 403


def test_an_UNKNOWN_stage_is_still_404(producer: TestClient) -> None:
    assert producer.get(_show("stage-nope"), headers=_bearer("alice")).status_code == 404


def _runner_without_the_field() -> FastAPI:
    """A stage runner build whose status body carries no `project` key at all — a mixed rollout."""
    app = FastAPI()
    app.state.workflow_client = _Workflows({})

    @app.get("/stages/{instance_id}")
    async def show(instance_id: str) -> dict[str, object]:
        return {"instance_id": instance_id, "status": "RUNNING", "submission_id": None, "polls_done": 0}

    @app.post("/stages/{instance_id}/terminate", status_code=202)
    async def stop(instance_id: str) -> dict[str, str]:
        app.state.workflow_client.terminated.append(instance_id)
        return {"instance_id": instance_id, "detail": "stopped"}

    return app


@pytest.mark.parametrize(
    "runner", [lambda: _stage_runner({"stage-x": _State("not json", name="stage_run")}), _runner_without_the_field], ids=["unreadable-input", "older-build"]
)
def test_a_stage_whose_tenant_cannot_be_read_is_REFUSED_to_a_person(fga: _Fga, monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    """Nothing to authorize on. Reading it as single-tenant would hand a tenant's run to acme's admins."""
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    app = runner()
    with TestClient(_producer(app), raise_server_exceptions=False) as client:
        refused = client.post(_stop("stage-x") + "?project=acme", headers=_bearer("bob"))
        served = client.get(_show("stage-x"), headers=SERVICE)

    assert refused.status_code == 503, refused.text
    assert _terminated(app) == []
    assert served.status_code == 200, "the service path needs no tenant and must not lose the run"


def test_the_SERVICE_path_is_unchanged_on_the_stage_doors(producer: TestClient, stage_runner: FastAPI) -> None:
    """The shared token is decided whole at the door: it needs no project to act on a run."""
    assert producer.get(_show("stage-other"), headers=SERVICE).status_code == 200
    assert producer.post(_stop("stage-other"), headers=SERVICE).status_code == 202
    assert _terminated(stage_runner) == ["stage-other"]


def test_an_UNWIRED_authorization_service_refuses_a_person_THEIR_OWN_stage(unwired: TestClient, stage_runner: FastAPI) -> None:
    """alice administers `mine`, so only the missing client can refuse her, and it must, as 503."""
    shown = unwired.get(_show("stage-mine"), headers=_bearer("alice"))
    stopped = unwired.post(_stop("stage-mine"), headers=_bearer("alice"))

    assert (shown.status_code, stopped.status_code) == (503, 503), (shown.text, stopped.text)
    assert _terminated(stage_runner) == []


def test_a_trigger_that_is_not_a_MAPPING_names_no_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    spec = StageJobSpec(from_uri="s3://wh/in", to_uri="s3://wh/out", stage="gold").model_dump() | {"trigger": "tok-1"}
    with TestClient(_stage_runner({"stage-odd": _State(spec, name="stage_run")})) as client:
        body = client.get("/stages/stage-odd", headers=SERVICE).json()

    assert body["project"] is None, "a trigger the stage runner cannot read is not a single-tenant one"


def _runner_reporting(project: object) -> FastAPI:
    """A stage runner whose status names ``project`` verbatim."""
    app = FastAPI()
    app.state.workflow_client = _Workflows({})

    @app.get("/stages/{instance_id}")
    async def show(instance_id: str) -> dict[str, object]:
        return {"instance_id": instance_id, "status": "RUNNING", "submission_id": None, "polls_done": 0, "project": project}

    @app.post("/stages/{instance_id}/terminate", status_code=202)
    async def stop(instance_id: str) -> dict[str, str]:
        app.state.workflow_client.terminated.append(instance_id)
        return {"instance_id": instance_id, "detail": "stopped"}

    return app


def test_a_stage_whose_recorded_project_is_UNSAFE_is_refused_to_a_person(fga: _Fga) -> None:
    """A project id no mint rule issues is nothing to authorize on, and never an FGA object."""
    app = _runner_reporting("../acme")
    with TestClient(_producer(app), raise_server_exceptions=False) as client:
        refused = client.post(_stop("stage-x"), headers=_bearer("bob"))

    assert refused.status_code == 503, refused.text
    assert _terminated(app) == []
    assert fga.calls == []


# ── the stalled-tier read ───────────────────────────────────────────────────────────────────────────


def _projects(response: httpx.Response) -> list[str]:
    return [cell["project"] for cell in response.json()["unpublished_source"]]


@pytest.mark.parametrize("query", ["", "?project=mine"])
def test_an_admin_sees_ONLY_the_stalled_cells_of_projects_they_administer(producer: TestClient, query: str) -> None:
    response = producer.get("/cascade/stalled" + query, headers=_bearer("alice"))

    assert response.status_code == 200, response.text
    assert _projects(response) == ["mine"]


def test_the_configured_projects_admin_sees_the_configured_projects_cells(producer: TestClient) -> None:
    assert _projects(producer.get("/cascade/stalled", headers=_bearer("bob"))) == [CONFIGURED, CONFIGURED]


def test_a_caller_administering_NONE_gets_an_empty_answer(producer: TestClient) -> None:
    """Empty rather than 403, like ingest's cross-tenant listing: a 403 for the whole call says that
    somebody's cascade has stalled."""
    response = producer.get("/cascade/stalled", headers=_bearer("carol"))

    assert response.status_code == 200, response.text
    assert _projects(response) == []


def test_the_filter_is_ONE_round_trip_over_the_distinct_projects(producer: TestClient, fga: _Fga) -> None:
    producer.get("/cascade/stalled", headers=_bearer("alice"))

    assert fga.calls == [("batch_check", ["project:acme", "project:mine", "project:other"])]


def test_an_authz_OUTAGE_is_503_never_an_empty_answer(producer: TestClient, fga: _Fga) -> None:
    fga.down = True

    assert producer.get("/cascade/stalled", headers=_bearer("alice")).status_code == 503


def test_the_SERVICE_path_still_reads_every_cell(producer: TestClient) -> None:
    assert _projects(producer.get("/cascade/stalled", headers=SERVICE)) == [CONFIGURED, CONFIGURED, "other", "mine"]


def test_a_caller_administering_NONE_is_answered_WITHOUT_a_measurement(producer: TestClient, ticks: _Ticks) -> None:
    """The tick is a catalog and a lineage read per edge under the service's own credentials; a caller
    holding no grant anywhere must not be able to spend it."""
    response = producer.get("/cascade/stalled", headers=_bearer("carol"))

    assert response.status_code == 200, response.text
    assert ticks.measured == []


def test_the_tick_measures_ONLY_the_callers_own_edges(producer: TestClient, ticks: _Ticks) -> None:
    producer.get("/cascade/stalled", headers=_bearer("alice"))

    assert ticks.measured == [[("bronze->silver", "mine")]]


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


# ── the training watch ──────────────────────────────────────────────────────────────────────────────


def test_the_configured_projects_admin_sees_its_training_watch(producer: TestClient) -> None:
    assert producer.get("/trains/train-acme", headers=_bearer("bob")).status_code == 200


def test_a_training_watch_is_gated_on_the_project_it_RECORDS(producer: TestClient) -> None:
    """The recorded project decides, not the deployment's: a watch naming `other` is refused to acme's
    admin, and one naming `mine` is shown to mine's."""
    assert producer.get("/trains/train-other", headers=_bearer("bob")).status_code == 403
    assert producer.get("/trains/train-mine", headers=_bearer("alice")).status_code == 200


def test_a_training_watch_of_another_project_CANNOT_BE_STOPPED(producer: TestClient) -> None:
    response = producer.post("/trains/train-other/terminate", headers=_bearer("bob"))

    assert response.status_code == 403, response.text
    assert _terminated(cast("FastAPI", producer.app)) == []


def test_an_UNWIRED_authorization_service_refuses_a_person_THEIR_OWN_watch(unwired: TestClient) -> None:
    assert unwired.get("/trains/train-acme", headers=_bearer("bob")).status_code == 503
    assert unwired.post("/trains/train-acme/terminate", headers=_bearer("bob")).status_code == 503
    assert _terminated(cast("FastAPI", unwired.app)) == []


def test_a_training_watch_whose_input_cannot_be_read_is_REFUSED_to_a_person(producer: TestClient) -> None:
    """Unreadable is not the configured project: reading it so would hand an unknown run to acme's admins."""
    assert producer.get("/trains/train-garbled", headers=_bearer("bob")).status_code == 503
    assert producer.get("/trains/train-garbled", headers=SERVICE).status_code == 200, "the service path needs no tenant"


def test_a_training_stop_is_logged_with_WHO_asked(producer: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=train_api.log.name):
        assert producer.post("/trains/train-acme/terminate", headers=_bearer("bob")).status_code == 202

    [line] = [r for r in caplog.records if r.getMessage() == "medallion_train_watch_termination_requested"]
    assert (line.__dict__["instance_id"], line.__dict__["subject"]) == ("train-acme", "bob")


def test_a_training_watch_RECORDS_the_configured_project_whatever_the_trigger_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the gate above only ever meets the configured project in production: the consumer stamps
    the deployment's project on the watch, and a trigger's own `project` claim does not reach it."""
    scheduled: dict[str, Any] = {}

    async def submitted(*_a: object, **_kw: object) -> str:
        return "submitted"

    def schedule(_settings: MedallionSettings, *, token: str, model: str, originator: str = "", project: str = "") -> str | None:
        scheduled["project"] = project
        return None

    monkeypatch.setattr(train_service.ray_submit, "submit_train_job", submitted)
    monkeypatch.setattr(train_service, "schedule_train_watch", schedule)
    settings = MedallionSettings.model_validate(
        {
            "MEDALLION_RAY_ENABLED": "true",
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
            "MEDALLION_BRONZE_URI": "s3://lake/medallion/bronze",
            "MEDALLION_PRODUCE_ADMIN_PROJECT": "globex",
        }
    )
    trigger = {"token": "tok-1", "model": "churn", "features": [{"dataset": "silver$features", "version": 7}], "config": {}, "project": "other"}

    result = asyncio.run(train_service.handle_train_trigger(settings, {"data": trigger}))

    assert result["status"] == "SUCCESS", result
    assert scheduled["project"] == "globex"


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


def _decisions(records: list[logging.LogRecord]) -> list[tuple[object, object, object]]:
    fields = [{k: v for k, v in r.__dict__.items() if k.startswith("audit.")} for r in records]
    return [(f["audit.outcome"], f["audit.subject"], f["audit.resource"]) for f in fields if f.get("audit.action") == "can_administer"]


def test_a_refused_stage_decision_names_the_STAGES_project(producer: TestClient, audited: list[logging.LogRecord]) -> None:
    producer.post(_stop("stage-other") + "?project=mine", headers=_bearer("alice"))

    assert _decisions(audited) == [("deny", "alice", "project:other")]


def test_a_training_watch_decision_names_the_WATCHS_project(producer: TestClient, audited: list[logging.LogRecord]) -> None:
    producer.get("/trains/train-other", headers=_bearer("bob"))

    assert _decisions(audited) == [("deny", "bob", "project:other")]


def test_the_stalled_filter_audits_one_decision_per_project(producer: TestClient, audited: list[logging.LogRecord]) -> None:
    producer.get("/cascade/stalled", headers=_bearer("alice"))

    assert _decisions(audited) == [("deny", "alice", "project:acme"), ("allow", "alice", "project:mine"), ("deny", "alice", "project:other")]


# ── which doors may take `?project=` at all ─────────────────────────────────────────────────────────

#: The producer operations whose `?project=` names what the call WRITES, or a surface no tenant owns.
#: Every other door acts on something that already exists and reads the tenant off it, so a new door
#: that declares the parameter fails here until someone decides which kind it is.
_PROJECT_PARAM_ALLOWED = {
    ("post", "/produce"): "the write target: the cascade head seeds THIS project's bronze",
    ("get", "/stage-runners"): "the deployment's stage-runner names, identical for every tenant",
}


def _declares_project(operation: dict[str, Any]) -> bool:
    return any(p.get("in") == "query" and p.get("name") == "project" for p in operation.get("parameters", []))


def test_only_WRITE_TARGET_doors_take_a_caller_chosen_project() -> None:
    from medallion.producer import app

    declared = {(method, path) for path, item in app.openapi()["paths"].items() for method, op in item.items() if _declares_project(op)}

    assert declared == set(_PROJECT_PARAM_ALLOWED), f"doors taking ?project= beyond the classified set: {sorted(declared - set(_PROJECT_PARAM_ALLOWED))}"


def test_the_producer_serves_no_admin_PROBE() -> None:
    """`GET /authorize` had no caller anywhere; owner ruling 2026-09-25 deletes it."""
    from medallion.producer import app

    assert "/authorize" not in app.openapi()["paths"]
