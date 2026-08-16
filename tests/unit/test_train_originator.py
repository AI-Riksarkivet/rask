"""The `/train` identity chain: the person who asked for a training run is named when it ends.

THE GAP (register rows #207 → #195/#206). `/train` is the estate's most expensive door — hours of GPU
on a submit-and-ack contract — and it was the only lane where the requester was verified and then
thrown away. `authorize_train` declared `-> None`, discarding the sub `authorize_produce` had already
returned; the trigger carried `{token, model, features, config}` and no requester; the Ray job's own
RunEvents carried `{lance: {operation, token}}`. So a training run that FAILED after four hours
reached nobody: `notifiable()` drops it at rule 2 (no verified author), and rule 3 (no `lance.project`)
would have cost every watcher too.

WHY `lance.originator` AND NOT `author`. The job posts its lifecycle to the lineage ingest as
`service-trainer`, and `enforce_author` OVERWRITES the author facet with that verified service sub —
correctly, because "never trust the request body" is what stops a producer forging someone else's
identity. So the human cannot be the author here and must not try to be. `originator` is the field
built for exactly this shape: a run authored by a service that is nevertheless FOR a person. It is a
TARGETING hint and authorizes nothing — the notifications plane re-derives each recipient's visibility
at delivery — which is why carrying it across the bus needs no new trust.

The chain is five links, and it delivers only if every one holds; each test below is one link.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from medallion.api import produce_auth
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, train
from openfga_sdk import OpenFgaClient


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_train_job.py"


def _load_job() -> ModuleType:
    """The job is a standalone script baked into the ray image, so it loads by path (as
    `test_train_job.py` does) — it must not become importable as a package."""
    spec = importlib.util.spec_from_file_location("ray_train_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {
        "MEDALLION_RAY_ENABLED": "true",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
        "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
        "MEDALLION_BRONZE_URI": "s3://lake/medallion/bronze",
    }
    values.update(overrides)
    return MedallionSettings.model_validate(values)


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_kw: Any) -> None:
        self.published.append({"pubsub": pubsub_name, "topic": topic_name, "data": data})


# ── link 1: the door keeps the sub ───────────────────────────────────────────────────────────────


class _Verifier:
    def __init__(self, sub: str) -> None:
        self._sub = sub

    def verify(self, _token: str) -> object:
        return SimpleNamespace(sub=self._sub)


def _run_authorize_train(monkeypatch: pytest.MonkeyPatch, *, app_token: str, authz: str | None, dapr_token: str | None = None) -> str | None:
    monkeypatch.setenv("APP_API_TOKEN", app_token)

    async def allow(_client: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr(produce_auth.fga, "check", allow)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=_Verifier("alice")))))
    settings = cast(MedallionSettings, SimpleNamespace(oidc_enabled=True, produce_admin_project="acme"))
    return asyncio.run(
        produce_auth.authorize_train(
            request,
            settings,
            cast(OpenFgaClient, object()),
            dapr_api_token=dapr_token,
            authorization=authz,
            dapr_caller_app_id=None,
        )
    )


def test_the_train_door_returns_the_verified_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/train` delegates its whole decision to `authorize_produce`, which already returns the sub —
    and then dropped it on the floor. The delegation is what makes the two doors one door, so the
    RETURN has to be delegated too, not just the checks."""
    assert _run_authorize_train(monkeypatch, app_token="secret", authz="Bearer t") == "alice"


def test_a_service_triggered_training_run_names_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared service token authenticates a SERVICE. `None`, never a placeholder: an inbox addressed
    to a role is precisely the defect this chain removes."""
    assert _run_authorize_train(monkeypatch, app_token="secret", authz=None, dapr_token="secret") is None


# ── link 2: the head puts it on the trigger ──────────────────────────────────────────────────────


def test_the_head_carries_the_originator_onto_the_training_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The head is the last place the request exists. By the time the job fails — hours later, on
    another machine, with no request in flight — the trigger is the only carrier left."""
    monkeypatch.setattr(train, "_resolve_version", lambda *_a: 7)
    dapr = _FakeDapr()
    asyncio.run(
        train.submit_train_request(
            cast(Any, dapr),
            _settings(),
            model="churn",
            features=[{"dataset": "silver$features"}],
            token="t1",
            originator="alice",
        )
    )
    assert json.loads(dapr.published[0]["data"])["originator"] == "alice"


def test_the_head_omits_the_originator_when_there_is_no_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string is not an identity, and a reader downstream must never mistake one for a
    person. Same rule the stage submission already applies to its metadata."""
    monkeypatch.setattr(train, "_resolve_version", lambda *_a: 7)
    dapr = _FakeDapr()
    asyncio.run(train.submit_train_request(cast(Any, dapr), _settings(), model="churn", features=[{"dataset": "silver$features"}], token="t1"))
    assert "originator" not in json.loads(dapr.published[0]["data"])


# ── link 3: the consumer forwards it to the submitter ────────────────────────────────────────────


def test_the_consumer_forwards_the_originator_to_the_ray_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigger arrives off the bus, so the originator is an untrusted CLAIM — carried, never
    trusted. It authorizes nothing here and is re-checked against visibility at delivery, the same
    posture `StageTrigger.originator` already documents."""
    seen: dict[str, Any] = {}

    async def fake_submit(_s: Any, **kw: Any) -> str:
        seen.update(kw)
        return "submitted"

    monkeypatch.setattr(train.ray_submit, "submit_train_job", fake_submit)
    event = {"data": {"token": "t1", "model": "churn", "features": [{"dataset": "silver$features", "version": 7}], "originator": "alice"}}
    assert asyncio.run(train.handle_train_trigger(_settings(), event)) == {"status": "SUCCESS"}
    assert seen["originator"] == "alice"


# ── link 4: the submission names the human where a failure can READ it ───────────────────────────


class _FakeJobsAPI:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeJobsAPI:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, _url: str, json: dict[str, Any]) -> Any:
        self.posts.append(json)
        return httpx.Response(200, request=httpx.Request("POST", "http://ray"))


def _submit(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> _FakeJobsAPI:
    api = _FakeJobsAPI()
    monkeypatch.setattr(ray_submit.httpx, "AsyncClient", lambda **_kw: api)
    asyncio.run(
        ray_submit.submit_train_job(
            _settings(),
            model="churn",
            features_json="[]",
            token="tok1",
            registry_uri="s3://lake/medallion/models/churn",
            artifact_base="s3://lake/models/churn",
            **kw,
        )
    )
    return api


def test_the_training_submission_names_the_human_in_rays_own_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """`metadata`, not only `runtime_env.env_vars`, and the distinction is the whole point: the
    identity has to be readable from OUTSIDE the job AFTER it has failed, and `metadata` is what comes
    back on `GET /api/jobs/<id>`. The env var is the job's own copy, for the events it emits itself."""
    api = _submit(monkeypatch, originator="alice", project="acme")
    assert api.posts[0]["metadata"]["rask.originator"] == "alice"
    assert api.posts[0]["metadata"]["rask.project"] == "acme"
    env = api.posts[0]["runtime_env"]["env_vars"]
    assert env["ORIGINATOR"] == "alice" and env["TRAIN_PROJECT"] == "acme"


def test_a_personless_training_submission_carries_no_empty_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _submit(monkeypatch)
    assert "rask.originator" not in api.posts[0]["metadata"]


# ── link 5: the job stamps it on the events it emits itself ──────────────────────────────────────


def test_the_training_job_stamps_the_originator_and_project_on_its_own_events() -> None:
    """The last link, and the one that actually delivers. The job authenticates as `service-trainer`,
    so `enforce_author` stamps THAT as the author — `lance.originator` is the only field on this event
    that can name the person, and `lance.project` is the only one that can reach a watcher."""
    job = _load_job()
    event = job.build_event(
        event_type="FAIL",
        token="t1",
        model="churn",
        namespace="models",
        features=[{"dataset": "silver$features", "version": 7}],
        error="CUDA out of memory",
        originator="alice",
        project="acme",
    )
    lance_facet = event["run"]["facets"]["lance"]
    assert lance_facet["originator"] == "alice"
    assert lance_facet["project"] == "acme"


def test_the_training_job_omits_both_when_it_has_neither() -> None:
    """A service-triggered run has no person behind it. The keys are ABSENT rather than empty:
    `originator_subject` reads truthiness, and an empty string would address an inbox named ''."""
    job = _load_job()
    event = job.build_event(event_type="START", token="t1", model="churn", namespace="models", features=[])
    lance_facet = event["run"]["facets"]["lance"]
    assert "originator" not in lance_facet and "project" not in lance_facet
