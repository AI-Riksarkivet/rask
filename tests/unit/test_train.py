"""Ray TRAIN head + trainer consumer (#115a, docs/RAY-TRAIN.md D1/D2/D5) — infra-free unit tier.

Covers the DONE WHEN unit items: the head publishes the pinned trigger (LATEST resolved AT the head),
the token guard is wired on /train, the consumer gates as the trainer identity (deny → DROP, outage →
RETRY), submit-and-ack semantics (bounded, re-attach on redelivery, NO resubmit of a terminally FAILED
prior job), and transport failure → RETRY.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError

from medallion.api.dependencies import get_dapr, get_settings
from medallion.api.train import router
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, train
from service_kit.lakehouse.ns_errors import install_problem_handlers


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


# --------------------------------------------------------------------------- #
# the head: version pinning + trigger publish + route wiring
# --------------------------------------------------------------------------- #


def test_stage_uri_derives_the_sibling_stage_from_the_bronze_uri() -> None:
    assert train.stage_uri_for(_settings(), "silver$features") == "s3://lake/medallion/silver"
    assert train.stage_uri_for(_settings(), "gold$catalog") == "s3://lake/medallion/gold"


def test_registry_and_artifact_layout_derivation() -> None:
    # D4: registry dataset beside the stages; artifact bytes in a SEPARATE tree at the bucket root
    # (never inside a Lance dataset directory — GC/orphan safety + the #92 allowlist prefix).
    assert train.registry_uri_for(_settings(), "churn") == "s3://lake/medallion/models/churn"
    assert train.artifact_base_for(_settings(), "churn") == "s3://lake/models/churn"
    local = _settings(MEDALLION_BRONZE_URI="/data/medallion/bronze")
    assert train.registry_uri_for(local, "churn") == "/data/medallion/models/churn"
    assert train.artifact_base_for(local, "churn") == "/data/medallion/model-artifacts/churn"


def test_head_resolves_omitted_versions_at_submit_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # D1: an omitted version pins to LATEST *here* — the trigger never carries a floating version.
    monkeypatch.setattr(train, "_resolve_version", lambda _s, dataset: {"silver$features": 7}[dataset])
    dapr = _FakeDapr()
    result = asyncio.run(
        train.submit_train_request(
            cast(Any, dapr),
            _settings(),
            token="idem-test",
            model="churn",
            features=[{"dataset": "silver$features"}, {"dataset": "gold$catalog", "version": 3}],
        )
    )
    assert result["features"] == [
        {"dataset": "silver$features", "version": 7},  # resolved
        {"dataset": "gold$catalog", "version": 3},  # caller's pin respected verbatim
    ]
    payload = json.loads(dapr.published[0]["data"])
    assert dapr.published[0]["topic"] == "training.jobs"  # the DEDICATED topic (D1)
    assert payload["model"] == "churn" and payload["token"] == result["token"]


def test_submit_train_request_reuses_idempotency_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # REGRESSION (bug hunt 2026-07-13): a caller-supplied token (the route's 503-retry Idempotency-Key) is
    # REUSED, so an ambiguous publish-timeout retry converges on the same deterministic run_ids (the graph
    # MERGEs the duplicate) instead of double-firing an unrelated training run. Absent → a fresh token.
    monkeypatch.setattr(train, "_resolve_version", lambda _s, _d: 1)
    dapr = _FakeDapr()
    result = asyncio.run(
        train.submit_train_request(
            cast(Any, dapr),
            _settings(),
            model="churn",
            features=[{"dataset": "silver$features"}],
            token="retry-key-1",
        )
    )
    assert result["token"] == "retry-key-1"
    assert json.loads(dapr.published[0]["data"])["token"] == "retry-key-1"


def test_head_surfaces_resolution_and_publish_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_s: Any, _d: str) -> int:
        raise RuntimeError("no such dataset")

    monkeypatch.setattr(train, "_resolve_version", boom)
    result = asyncio.run(train.submit_train_request(cast(Any, _FakeDapr()), _settings(), token="idem-test", model="m", features=[{"dataset": "nope$x"}]))
    assert result == {"status": "resolve_failed", "dataset": "nope$x"}

    class _BoomDapr:
        async def publish_event(self, **_kw: Any) -> None:
            raise RuntimeError("sidecar down")

    monkeypatch.setattr(train, "_resolve_version", lambda _s, _d: 1)
    result = asyncio.run(
        train.submit_train_request(cast(Any, _BoomDapr()), _settings(), token="idem-test", model="m", features=[{"dataset": "silver$features"}])
    )
    assert result["status"] == "publish_failed"


def test_train_route_enforces_the_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    app = FastAPI()
    # Same problem+json handlers the producer installs: the guard raises the lance_namespace domain
    # errors (PermissionDeniedError), which a bare FastAPI() would surface as 500 rather than 403.
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: _settings()
    client = TestClient(app, raise_server_exceptions=False)
    body = {"model": "m", "features": [{"dataset": "silver$features"}]}
    assert client.post("/train", json=body).status_code == 403
    assert client.post("/train", json=body, headers={"dapr-api-token": "nope"}).status_code == 403


def test_train_route_409_when_the_head_is_not_configured() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: _settings(MEDALLION_RAY_ENABLED="false")
    response = TestClient(app).post("/train", json={"model": "m", "features": [{"dataset": "a$b"}]}, headers={"Idempotency-Key": "idem-test"})
    assert response.status_code == 409  # explicit contract, never a silent 202


# --------------------------------------------------------------------------- #
# the consumer: FGA gates (D5) + submit-and-ack (D2)
# --------------------------------------------------------------------------- #

_EVENT = {
    "data": {
        "token": "t1",
        "model": "churn",
        "features": [{"dataset": "silver$features", "version": 7}],
    }
}


def _gate(monkeypatch: pytest.MonkeyPatch, allowed: dict[str, bool]) -> None:
    """Patch BOTH gate seams: inputs go through ONE fga.batch_check round trip (ack-window bound,
    review 2026-07-11), the models-namespace rung through fga.check."""

    async def check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        assert user == "service-trainer"  # the trainer's OWN identity, never the mover rung (D5)
        return allowed[f"{relation}:{obj}"]

    async def batch(_client: Any, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        assert user == "service-trainer"
        return {obj: allowed[f"{relation}:{obj}"] for obj in objects}

    monkeypatch.setattr(train.fga, "check", check)
    monkeypatch.setattr(train.fga, "batch_check", batch)


def test_consumer_denied_input_or_models_rung_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(
        train.ray_submit,
        "submit_train_job",
        lambda *a, **k: submitted.append("x"),  # never awaited
    )
    _gate(monkeypatch, {"can_read_data:table:silver$features": False})
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object()))
    assert result == {"status": "DROP"} and submitted == []  # denied BEFORE any compute is spent

    _gate(
        monkeypatch,
        {"can_read_data:table:silver$features": True, "can_create_table:namespace:models": False},
    )
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object()))
    assert result == {"status": "DROP"} and submitted == []


def test_consumer_fga_outage_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def outage(*_a: Any, **_kw: Any) -> bool:
        raise ServiceUnavailableError("fga down")

    monkeypatch.setattr(train.fga, "check", outage)
    monkeypatch.setattr(train.fga, "batch_check", outage)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object()))
    assert result == {"status": "RETRY"}  # outage ≠ denial


def test_consumer_seeds_the_model_parent_link_before_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    # #115c: without `namespace:models parent table:models$<m>` no human rung cascades to the
    # registry dataset — the published model would be invisible under RASK_FGA_ENABLED. The
    # consumer writes it idempotently BEFORE the submit ack; an outage on the write → RETRY.
    written: list[Any] = []

    async def fake_write(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        written.extend(tuples)

    async def fake_submit(*_a: Any, **_kw: Any) -> str:
        return "submitted"

    _gate(
        monkeypatch,
        {"can_read_data:table:silver$features": True, "can_create_table:namespace:models": True},
    )
    monkeypatch.setattr(train.fga, "write_tuples", fake_write)
    monkeypatch.setattr(train.ray_submit, "submit_train_job", fake_submit)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object()))
    assert result == {"status": "SUCCESS"}
    assert (written[0].user, written[0].relation, written[0].object) == (
        "namespace:models",
        "parent",
        "table:models$churn",
    )

    async def outage(*_a: Any, **_kw: Any) -> None:
        raise ServiceUnavailableError("fga down")

    monkeypatch.setattr(train.fga, "write_tuples", outage)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object()))
    assert result == {"status": "RETRY"}


def test_consumer_submits_and_acks_and_maps_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = iter(["submitted", "attached", "already_failed"])
    calls: list[str] = []

    async def fake_submit(_s: Any, *, model: str, features_json: str, token: str, **kw: Any) -> str:
        calls.append(token)
        # #115b: the consumer enriches each pinned feature with its Lance URI and derives the D4
        # publish pointers — the job reads these verbatim (layout convention lives in train.py only).
        assert json.loads(features_json) == [{"dataset": "silver$features", "version": 7, "uri": "s3://lake/medallion/silver"}]
        assert kw["registry_uri"] == "s3://lake/medallion/models/churn"
        assert kw["artifact_base"] == "s3://lake/models/churn"
        return next(outcomes)

    monkeypatch.setattr(train.ray_submit, "submit_train_job", fake_submit)
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT)) == {"status": "SUCCESS"}
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT)) == {"status": "SUCCESS"}  # re-attach
    # a terminally FAILED prior job is DROPPED — training is never auto-resubmitted (D2)
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT)) == {"status": "DROP"}
    assert calls == ["t1", "t1", "t1"]

    async def transport_error(*_a: Any, **_kw: Any) -> str:
        raise ray_submit.RayJobError("submit failed")

    monkeypatch.setattr(train.ray_submit, "submit_train_job", transport_error)
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT)) == {"status": "RETRY"}

    assert asyncio.run(train.handle_train_trigger(_settings(), {"data": {}})) == {"status": "DROP"}


def test_consumer_drops_unpinned_or_empty_features(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review 2026-07-10: a version-less feature would train on floating LATEST (violates D1) and an
    # empty-after-filter list would gate vacuously — both are malformed triggers: DROP, never repair.
    async def never(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not submit")

    monkeypatch.setattr(train.ray_submit, "submit_train_job", never)
    unpinned = {"data": {"token": "t", "model": "m", "features": [{"dataset": "silver$features"}]}}
    assert asyncio.run(train.handle_train_trigger(_settings(), unpinned)) == {"status": "DROP"}
    junk = {"data": {"token": "t", "model": "m", "features": ["junk"]}}
    assert asyncio.run(train.handle_train_trigger(_settings(), junk)) == {"status": "DROP"}
    empty = {"data": {"token": "t", "model": "m", "features": []}}
    assert asyncio.run(train.handle_train_trigger(_settings(), empty)) == {"status": "DROP"}


def test_consumer_drops_path_unsafe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # #115b: model/token/dataset from the BUS become S3 key prefixes and Lance URIs — a traversal-shaped
    # or separator-carrying name is a malformed trigger, DROPped before any URI is derived.
    async def never(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not submit")

    monkeypatch.setattr(train.ray_submit, "submit_train_job", never)
    ok = {"dataset": "silver$features", "version": 7}
    for data in (
        {"token": "t1", "model": "../etc", "features": [ok]},
        {"token": "a/b", "model": "churn", "features": [ok]},
        {"token": "t1", "model": "churn", "features": [{"dataset": "silver$../evil", "version": 1}]},
        {"token": "t1", "model": "churn", "features": [{"dataset": "a$b$c", "version": 1}]},
        # a BARE dataset name is rejected too: it would derive a wrong stage URI AND (in the job's
        # lineage) a namespace equal to the whole name, corrupting the shared graph node's namespace
        {"token": "t1", "model": "churn", "features": [{"dataset": "events", "version": 1}]},
    ):
        assert asyncio.run(train.handle_train_trigger(_settings(), {"data": data})) == {"status": "DROP"}


def test_consumer_drops_oversized_or_nondict_config_and_too_many_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2026-07-11: the head's claim-check bound must hold at the CONSUMER too — the bus is a
    # wider trust surface, and config flows verbatim into the Ray Jobs runtime_env.
    async def never(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("must not submit")

    monkeypatch.setattr(train.ray_submit, "submit_train_job", never)
    ok = {"dataset": "silver$features", "version": 7}
    huge = {"blob": "x" * (train._MAX_CONFIG_BYTES + 1)}
    for data in (
        {"token": "t1", "model": "churn", "features": [ok], "config": huge},
        {"token": "t1", "model": "churn", "features": [ok], "config": ["not-a-dict"]},
        {"token": "t1", "model": "churn", "features": [ok] * (train.MAX_FEATURES + 1)},
    ):
        assert asyncio.run(train.handle_train_trigger(_settings(), {"data": data})) == {"status": "DROP"}


def test_train_route_422s_the_names_its_consumer_would_drop() -> None:
    # Review 2026-07-11: the head refuses what the consumer would DROP — never a 202 into a silent
    # no-op. Pydantic pattern/max_length gates mirror the consumer's _safe_name/_safe_dataset/cap.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: _settings()
    client = TestClient(app)
    ok_feature = {"dataset": "silver$features", "version": 1}
    for body in (
        {"model": "../etc", "features": [ok_feature]},
        {"model": "churn", "features": [{"dataset": "events", "version": 1}]},  # bare name
        {"model": "churn", "features": [{"dataset": "silver$../evil", "version": 1}]},
        {"model": "churn", "features": [ok_feature] * (train.MAX_FEATURES + 1)},
    ):
        assert client.post("/train", json=body).status_code == 422


def test_head_rejects_an_oversized_config() -> None:
    # Claim-check: the trigger carries pointers + hyperparams, never data-shaped content.
    result = asyncio.run(
        train.submit_train_request(
            cast(Any, _FakeDapr()),
            _settings(),
            token="idem-test",
            model="m",
            features=[{"dataset": "a$b", "version": 1}],
            config={"blob": "x" * 10_000},
        )
    )
    assert result == {"status": "config_too_large"}


# --------------------------------------------------------------------------- #
# submit_train_job against a fake Ray Jobs API — the D2 semantics at the transport
# --------------------------------------------------------------------------- #


class _FakeJobsAPI:
    """Programmable stand-in for httpx.AsyncClient against the Ray Jobs REST API."""

    def __init__(self, post_status: int, existing_status: str | None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self._post_status = post_status
        self._existing = existing_status

    async def __aenter__(self) -> _FakeJobsAPI:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, _url: str, json: dict[str, Any]) -> Any:
        self.posts.append(json)
        return httpx.Response(self._post_status, request=httpx.Request("POST", "http://ray"))

    async def get(self, url: str) -> Any:
        assert self._existing is not None, f"unexpected GET {url}"
        return httpx.Response(200, request=httpx.Request("GET", "http://ray"), json={"status": self._existing})

    async def delete(self, url: str) -> Any:  # pragma: no cover — MUST never be called for train
        raise AssertionError(f"train path must never DELETE a prior job (got {url})")


def _run_submit(monkeypatch: pytest.MonkeyPatch, api: _FakeJobsAPI) -> str:
    def make_client(**kw: Any) -> _FakeJobsAPI:
        # The ack-window bound: EVERY await inside submit_train_job runs under this client timeout —
        # a refactor dropping it would un-bound the handler against a hung Ray API (review 2026-07-10).
        assert kw["timeout"] == _settings().ray_request_timeout_seconds
        return api

    monkeypatch.setattr(ray_submit.httpx, "AsyncClient", make_client)
    return asyncio.run(
        ray_submit.submit_train_job(
            _settings(),
            model="churn",
            features_json="[]",
            token="tok1",
            registry_uri="s3://lake/medallion/models/churn",
            artifact_base="s3://lake/models/churn",
        )
    )


def test_submit_train_job_fresh_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakeJobsAPI(post_status=200, existing_status=None)
    assert _run_submit(monkeypatch, api) == "submitted"
    assert api.posts[0]["submission_id"] == "ray-train-tok1"  # deterministic idempotency key
    assert api.posts[0]["entrypoint"].endswith("ray_train_job.py")
    env = api.posts[0]["runtime_env"]["env_vars"]
    # #115b: the job's publish pointers + its lineage ingest travel in the job env verbatim.
    assert env["REGISTRY_URI"] == "s3://lake/medallion/models/churn"
    assert env["ARTIFACT_BASE"] == "s3://lake/models/churn"
    assert env["LINEAGE_URL"] == _settings().train_lineage_url


def test_submit_train_job_reattaches_to_a_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # Redelivery: the POST 4xxs (id exists), the job is RUNNING → attach, no second job, no delete.
    api = _FakeJobsAPI(post_status=409, existing_status="RUNNING")
    assert _run_submit(monkeypatch, api) == "attached"
    assert len(api.posts) == 1  # exactly one submit attempt — never a duplicate job


def test_submit_train_job_never_resubmits_a_failed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # D2: unlike the stage path (delete + fresh resubmit), a FAILED training job is terminal.
    api = _FakeJobsAPI(post_status=409, existing_status="FAILED")
    assert _run_submit(monkeypatch, api) == "already_failed"
    assert len(api.posts) == 1 and api.deletes == []
