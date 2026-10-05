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

The chain is five links, and it delivers only if every one holds; each section below pins one link, or two read off one submission.
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
from openfga_sdk import OpenFgaClient

from medallion.api import produce_auth
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, train


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


def _run_authorize_train(monkeypatch: pytest.MonkeyPatch, *, authz: str | None) -> str | None:
    async def allow(_client: object, **_kw: object) -> bool:
        return True

    monkeypatch.setattr(produce_auth.fga, "check", allow)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=_Verifier("alice"), sa_oidc=None))))
    settings = cast(MedallionSettings, SimpleNamespace(oidc_enabled=True, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=False))
    return asyncio.run(
        produce_auth.authorize_train(
            request,
            settings,
            cast(OpenFgaClient, object()),
            authorization=authz,
            dapr_caller_app_id=None,
        )
    )


def test_the_train_door_returns_the_verified_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/train` delegates its whole decision to `authorize_produce`, which already returns the sub —
    and then dropped it on the floor. The delegation is what makes the two doors one door, so the
    RETURN has to be delegated too, not just the checks."""
    assert _run_authorize_train(monkeypatch, authz="Bearer t") == "alice"


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


# ── links 3 and 4: the consumer's order names the human, where the job and a failure can both read it ───


class _Ray:
    """The Ray Jobs API behind the Ray adapter: accepts every submission and keeps its body."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        return httpx.Response(200)


def _consume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **trigger: Any) -> dict[str, Any]:
    """One training trigger through the producer's consumer; the body its order reached Ray with."""
    ray = _Ray()
    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(ray.handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    event = {"data": {"token": "t1", "model": "churn", "features": [{"dataset": "silver$features", "version": 7}], **trigger}}
    local = _settings(MEDALLION_BRONZE_URI=f"{tmp_path}/medallion/bronze", MEDALLION_CONTROL_ROOT=f"{tmp_path}/control", MEDALLION_PRODUCE_ADMIN_PROJECT="acme")
    assert asyncio.run(train.handle_train_trigger(local, event, dapr=_FakeDapr())) == {"status": "SUCCESS"}
    (body,) = ray.posts
    return body


def test_the_consumer_names_the_human_in_the_jobs_env_and_in_rays_own_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The trigger arrives off the bus, so the originator is an untrusted CLAIM — carried, never trusted. It
    authorizes nothing here and is re-checked against visibility at delivery, the same posture
    `StageTrigger.originator` already documents.

    `metadata`, not only `runtime_env.env_vars`, and the distinction is the whole point: the identity has to be
    readable from OUTSIDE the job AFTER it has failed, and `metadata` is what comes back on `GET /api/jobs/<id>`. The
    env var is the job's own copy, for the events it emits itself."""
    body = _consume(monkeypatch, tmp_path, originator="alice")

    assert (body["metadata"]["rask.originator"], body["metadata"]["rask.project"]) == ("alice", "acme")
    env = body["runtime_env"]["env_vars"]
    assert (env["RASK_ORIGINATOR"], env["RASK_PROJECT"]) == ("alice", "acme")


def test_a_personless_training_submission_carries_no_empty_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = _consume(monkeypatch, tmp_path)

    assert "rask.originator" not in body["metadata"]
    assert "RASK_ORIGINATOR" not in body["runtime_env"]["env_vars"]


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
    ).to_wire()
    lance_facet = event["run"]["facets"]["lance"]
    assert lance_facet["originator"] == "alice"
    assert lance_facet["project"] == "acme"


def test_the_training_job_omits_both_when_it_has_neither() -> None:
    """A service-triggered run has no person behind it. The keys are ABSENT rather than empty:
    `originator_subject` reads truthiness, and an empty string would address an inbox named ''."""
    job = _load_job()
    event = job.build_event(event_type="START", token="t1", model="churn", namespace="models", features=[]).to_wire()
    lance_facet = event["run"]["facets"]["lance"]
    assert "originator" not in lance_facet and "project" not in lance_facet
