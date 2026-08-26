"""An in-flight `stage_run` could be neither observed nor stopped by any HTTP means (DWF-MGT-002/003).

`stage_run` submits a Ray job and watches it for up to `MAX_POLLS`, and until this change there was no
route anywhere that could report on one or stop one. `services/compute` proxies Ray read-only and
knows nothing about the workflow doing the watching, and the movers mounted only `/healthz` and their
two event doors.

THE SPLIT IS FORCED, NOT CHOSEN, and these tests pin both halves because either alone is useless:

  * `terminate_workflow` and `get_workflow_state` resolve an instance through the CALLING app's
    app-id, and `stage_run` executes in the MOVER's runtime -- so the routes that touch the workflow
    must live there. A producer-hosted copy would look under `medallion-producer`, find nothing, and
    ACCEPT THE CALL ANYWAY: a 202 for a terminate that stopped nothing. `promotions.py` records that
    exact trap from the other direction.
  * a mover is bus-only -- no gateway row, no Ingress -- so a route hosted only there is a lever
    nobody can pull.

So the producer authorizes and forwards, and the mover does the work under its own app-id.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from medallion.api import mover_ops, stage_ops

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


def _mover_app(client: _Client | None) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(stage_ops.router)
    app.state.workflow_client = client
    # The service-token check has its own suite; overridden so these assertions are about the
    # management surface rather than the auth posture.
    app.dependency_overrides[stage_ops.require_dapr_token] = lambda: None
    return app


@pytest.fixture
def mover() -> Iterator[TestClient]:
    live = _Client({LIVE: _State({"submission_id": "ray-silver-tok-1", "polls_done": 4, "from_uri": "s3://wh/secret"}, WorkflowStatus.RUNNING)})
    with TestClient(_mover_app(live), raise_server_exceptions=False) as c:
        yield c


def test_a_live_cascade_stage_can_be_OBSERVED(mover: TestClient) -> None:
    body = mover.get(f"/stages/{LIVE}").json()

    assert body["status"] == "RUNNING"
    assert body["submission_id"] == "ray-silver-tok-1", "an operator must be able to cross-check the Ray dashboard"
    assert body["polls_done"] == 4


def test_the_status_read_does_NOT_disclose_the_spec(mover: TestClient) -> None:
    """`WorkflowState` carries the whole `StageJobSpec` -- the URIs and the lineage blob. A status
    question must not hand them over to answer."""
    body = mover.get(f"/stages/{LIVE}").json()

    assert set(body) == {"instance_id", "status", "submission_id", "polls_done"}, f"the wire shape widened: {sorted(body)}"
    assert "secret" not in json.dumps(body), "the spec leaked through the status route"


def test_a_live_cascade_stage_can_be_TERMINATED(mover: TestClient) -> None:
    resp = mover.post(f"/stages/{LIVE}/terminate")

    assert resp.status_code == 202, resp.text
    assert cast("Any", mover.app).state.workflow_client.terminated == [LIVE]


def test_the_terminate_body_REFUSES_to_imply_the_ray_job_stopped(mover: TestClient) -> None:
    """`stage_run` polls a job it submitted and cannot kill. The lever it DOES provide is stopping the
    next tier's trigger, and the body has to say which of the two it did."""
    detail = mover.post(f"/stages/{LIVE}/terminate").json()["detail"]

    assert "keeps running" in detail, f"an operator could read this as 'the GPUs are free': {detail!r}"
    assert "trigger" in detail, f"the body does not say what terminating actually buys: {detail!r}"


@pytest.mark.parametrize("call", [lambda c: c.get("/stages/nope"), lambda c: c.post("/stages/nope/terminate")])
def test_an_UNKNOWN_instance_is_404_on_both_verbs(mover: TestClient, call: Any) -> None:
    assert call(mover).status_code == 404


def test_terminating_an_unknown_instance_terminates_NOTHING(mover: TestClient) -> None:
    mover.post("/stages/nope/terminate")

    assert cast("Any", mover.app).state.workflow_client.terminated == []


@pytest.mark.parametrize("call", [lambda c: c.get(f"/stages/{LIVE}"), lambda c: c.post(f"/stages/{LIVE}/terminate")])
def test_no_engine_is_UNAVAILABLE_never_a_silent_success(call: Any) -> None:
    """503, not 202. A mover with no runtime cannot have the instance, and answering 202 would tell an
    operator a runaway was stopped when nothing was called."""
    with TestClient(_mover_app(None), raise_server_exceptions=False) as c:
        assert call(c).status_code == 503


# --------------------------------------------------------------------------------------------------
# The producer's half: the only door a person can reach
# --------------------------------------------------------------------------------------------------


def _producer_app(mover_urls: dict[str, str], transport: Any) -> FastAPI:
    from medallion.core.config import MedallionSettings

    app = FastAPI()
    register_handlers(app)
    app.include_router(mover_ops.router)
    app.state.medallion_settings = MedallionSettings().model_copy(update={"mover_urls": mover_urls})
    app.state.http = transport
    app.dependency_overrides[mover_ops.authorize_produce] = lambda: "CiQwOGE4Njg0Yi1kYjg4"
    app.dependency_overrides[mover_ops.SettingsDep.__metadata__[0].dependency] = lambda: app.state.medallion_settings
    return app


class _Recorder:
    """Stands in for the producer's lifespan httpx client, recording where it forwarded."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._status = status
        self._body = body if body is not None else {"instance_id": LIVE, "status": "RUNNING"}

    async def request(self, method: str, url: str, **_kw: Any) -> Any:
        self.calls.append((method, url))

        class _Resp:
            status_code = self._status
            text = json.dumps(self._body)

            def json(_s) -> Any:  # noqa: N805 — a stand-in, not a real method
                return self._body

        return _Resp()


def test_the_producer_forwards_to_the_MOVER_that_hosts_the_instance() -> None:
    """The whole reason this half exists: the terminate has to execute in the mover's process."""
    recorder = _Recorder()
    with TestClient(_producer_app({"silver-to-gold": "http://rask-silver-to-gold:8000"}, recorder), raise_server_exceptions=False) as c:
        resp = c.post(f"/movers/silver-to-gold/stages/{LIVE}/terminate")

    assert resp.status_code == 202, resp.text
    assert recorder.calls == [("POST", f"http://rask-silver-to-gold:8000/stages/{LIVE}/terminate")]


def test_an_UNCONFIGURED_mover_is_404_and_NAMES_what_is_configured() -> None:
    """404 and not 502: not-configured and unreachable are different operator problems, and the common
    cause is a name typo against a values-driven list."""
    with TestClient(_producer_app({"silver-to-gold": "http://x:8000"}, _Recorder()), raise_server_exceptions=False) as c:
        resp = c.get(f"/movers/bronze-to-silvr/stages/{LIVE}")

    assert resp.status_code == 404
    assert "silver-to-gold" in resp.text, f"the refusal does not name the movers that DO exist: {resp.text}"


def test_the_movers_can_be_LISTED_so_a_caller_need_not_guess_a_name() -> None:
    with TestClient(_producer_app({"b": "http://b:8000", "a": "http://a:8000"}, _Recorder()), raise_server_exceptions=False) as c:
        assert c.get("/movers").json()["movers"] == ["a", "b"]


def test_an_UNREACHABLE_mover_is_502_not_a_silent_success() -> None:
    import httpx

    class _Down:
        async def request(self, *_a: Any, **_k: Any) -> Any:
            raise httpx.ConnectError("refused")

    with TestClient(_producer_app({"m": "http://m:8000"}, _Down()), raise_server_exceptions=False) as c:
        assert c.post(f"/movers/m/stages/{LIVE}/terminate").status_code == 502
