"""Ray TRAIN head + trainer consumer (#115a, docs/RAY-TRAIN.md D1/D2/D5) — infra-free unit tier.

Covers the DONE WHEN unit items: the head publishes the pinned trigger (LATEST resolved AT the head),
the token guard is wired on /train, the consumer gates as the trainer identity (deny → DROP, outage →
RETRY), submit-and-ack semantics through the executor port (re-attach on redelivery, NO resubmit of a
terminally FAILED prior job), and transport failure → RETRY. The Ray Jobs API is the one stand-in: the
consumer's order reaches it through the real Ray adapter (CP-044).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientCheckRequest, ClientTuple, ClientWriteRequest
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_error import CheckError
from openfga_sdk.models.check_response import CheckResponse

from medallion.api.dependencies import get_dapr, get_settings
from medallion.api.train import router
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, train, train_plans
from service_kit.lakehouse.ns_errors import install_problem_handlers


#: This test's local estate, set per test by `_local_estate`: every training run is PLANNED into a control root and
#: reads its registry's version first (CP-029), so both live on `tmp_path` rather than behind an unreachable endpoint.
_ROOT = Path()


@pytest.fixture(autouse=True)
def _local_estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {
        "MEDALLION_RAY_ENABLED": "true",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
        "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
        "MEDALLION_BRONZE_URI": f"{_ROOT}/medallion/bronze",
        "MEDALLION_CONTROL_ROOT": f"{_ROOT}/control",
    }
    values.update(overrides)
    return MedallionSettings.model_validate(values)


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_kw: Any) -> None:
        self.published.append({"pubsub": pubsub_name, "topic": topic_name, "data": data})


class _Ray:
    """The Ray Jobs API behind the Ray adapter: each POST answered by the next ``(status, existing job state)`` of
    ``answers`` (a fresh 200 once they run out), every body kept, and a DELETE refused — the train lane never deletes
    a prior job (D2)."""

    def __init__(self, answers: list[tuple[int, str | None]] | None = None, *, unreachable: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self._answers = list(answers or [])
        self._existing: str | None = None
        self._unreachable = unreachable

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self._unreachable:
            raise httpx.ConnectError("ray head unreachable", request=request)
        if request.method == "POST":
            self.posts.append(json.loads(request.content))
            status, self._existing = self._answers.pop(0) if self._answers else (200, None)
            return httpx.Response(status)
        if request.method == "GET":
            return httpx.Response(200, json={"status": self._existing})
        raise AssertionError(f"the train lane must never {request.method} a prior job ({request.url})")

    def tokens(self) -> list[str]:
        return [post["runtime_env"]["env_vars"]["RASK_TOKEN"] for post in self.posts]


def _ray(monkeypatch: pytest.MonkeyPatch, ray: _Ray | None = None) -> _Ray:
    fake = ray or _Ray()
    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(fake.handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return fake


# --------------------------------------------------------------------------- #
# the head: version pinning + trigger publish + route wiring
# --------------------------------------------------------------------------- #


def test_registry_and_artifact_layout_derivation() -> None:
    # D4: registry dataset beside the stages; artifact bytes in a SEPARATE tree at the bucket root
    # (never inside a Lance dataset directory — GC/orphan safety + the #92 allowlist prefix).
    bucket = _settings(MEDALLION_BRONZE_URI="s3://lake/medallion/bronze")
    assert train.registry_uri_for(bucket, "churn") == "s3://lake/medallion/models/churn"
    assert train.artifact_base_for(bucket, "churn") == "s3://lake/models/churn"
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


def test_train_route_refuses_a_caller_nobody_vouched_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's root conftest acknowledges an open door by default; this one does not, so the door
    has nothing to authenticate a caller with and refuses. The `dapr-api-token` a sidecar stamps names
    nobody, so presenting one changes nothing."""
    monkeypatch.setenv("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "false")
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
    assert client.post("/train", json=body, headers={"dapr-api-token": "the-estate-app-token"}).status_code == 403


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
        assert user == "service-trainer"  # the trainer's OWN identity, never the stage runner rung (D5)
        return allowed[f"{relation}:{obj}"]

    async def batch(_client: Any, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        assert user == "service-trainer"
        return {obj: allowed[f"{relation}:{obj}"] for obj in objects}

    monkeypatch.setattr(train.fga, "check", check)
    monkeypatch.setattr(train.fga, "batch_check", batch)


def test_consumer_denied_input_or_models_rung_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    ray = _ray(monkeypatch)
    _gate(monkeypatch, {"can_read_data:table:silver$features": False})
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object(), dapr=_FakeDapr()))
    assert result["status"] == "DROP" and ray.posts == []  # denied BEFORE any compute is spent

    _gate(
        monkeypatch,
        {"can_read_data:table:silver$features": True, "can_create_table:namespace:models": False},
    )
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object(), dapr=_FakeDapr()))
    assert result["status"] == "DROP" and ray.posts == []


def test_consumer_fga_outage_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def outage(*_a: Any, **_kw: Any) -> bool:
        raise ServiceUnavailableError("fga down")

    monkeypatch.setattr(train.fga, "check", outage)
    monkeypatch.setattr(train.fga, "batch_check", outage)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object(), dapr=_FakeDapr()))
    assert result == {"status": "RETRY"}  # outage ≠ denial


class _TrainerOpenFga:
    """An OpenFGA client answering through the SDK's own response classes, so the REAL `fga.*` gate
    runs. Every input is readable except those in ``unanswered``, which BatchCheck returns as the SDK
    hands back an item the server could not evaluate: ``allowed=False`` beside a ``CheckError``."""

    def __init__(self, unanswered: set[str]) -> None:
        self._unanswered = unanswered
        self.written: list[Any] = []

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, Any] | None = None) -> ClientBatchCheckResponse:
        del options
        result = []
        for index, item in enumerate(body.checks):
            error = CheckError(input_error="validation_error", message="relation not found in the pinned model") if item.object in self._unanswered else None
            # The SDK hands the batch ITEM back as `request` (client.py `map_response`); its annotation says ClientTuple.
            result.append(ClientBatchCheckSingleResponse(allowed=error is None, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=error))
        return ClientBatchCheckResponse(result)

    async def check(self, body: ClientCheckRequest, options: dict[str, Any] | None = None) -> CheckResponse:
        del body, options
        return CheckResponse(allowed=True)

    async def write(self, body: ClientWriteRequest, options: dict[str, Any] | None = None) -> None:
        del options
        self.written.append(body)


def test_the_sdk_harness_trains_on_inputs_openfga_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this, the RETRY below could be a gate that never reached OpenFGA."""
    ray = _ray(monkeypatch)
    client = _TrainerOpenFga(unanswered=set())

    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=client, dapr=_FakeDapr())) == {"status": "SUCCESS"}
    assert ray.tokens() == ["t1"] and len(client.written) == 1


def test_an_input_openfga_could_not_answer_retries_rather_than_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DROP is terminal, and D2 never resubmits training, so an unanswered input read as a deny
    would lose the trigger to an OpenFGA fault as if the trainer lacked the grant."""
    ray = _ray(monkeypatch)
    client = _TrainerOpenFga(unanswered={"table:silver$features"})

    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=client, dapr=_FakeDapr())) == {"status": "RETRY"}
    assert ray.posts == [] and client.written == []


def test_consumer_seeds_the_model_parent_link_before_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    # #115c: without `namespace:models parent table:models$<m>` no human rung cascades to the
    # registry dataset — the published model would be invisible under RASK_FGA_ENABLED. The
    # consumer writes it idempotently BEFORE the submit ack; an outage on the write → RETRY.
    written: list[Any] = []

    async def fake_write(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        written.extend(tuples)

    _ray(monkeypatch)
    _gate(
        monkeypatch,
        {"can_read_data:table:silver$features": True, "can_create_table:namespace:models": True},
    )
    monkeypatch.setattr(train.fga, "write_tuples", fake_write)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object(), dapr=_FakeDapr()))
    assert result == {"status": "SUCCESS"}
    assert (written[0].user, written[0].relation, written[0].object) == (
        "namespace:models",
        "parent",
        "table:models$churn",
    )

    async def outage(*_a: Any, **_kw: Any) -> None:
        raise ServiceUnavailableError("fga down")

    monkeypatch.setattr(train.fga, "write_tuples", outage)
    result = asyncio.run(train.handle_train_trigger(_settings(), _EVENT, fga_client=object(), dapr=_FakeDapr()))
    assert result == {"status": "RETRY"}


def test_consumer_submits_and_acks_and_maps_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh submit, a redelivery re-attaching to the running job, and one finding the job FAILED.
    ray = _ray(monkeypatch, _Ray([(200, None), (409, "RUNNING"), (409, "FAILED")]))
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, dapr=_FakeDapr())) == {"status": "SUCCESS"}
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, dapr=_FakeDapr())) == {"status": "SUCCESS"}  # re-attach
    # a terminally FAILED prior job is DROPPED and never deleted — training is never auto-resubmitted (D2)
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, dapr=_FakeDapr()))["status"] == "DROP"
    assert ray.tokens() == ["t1", "t1", "t1"] and len({post["submission_id"] for post in ray.posts}) == 1
    # #115b: the consumer enriches each pinned feature with its Lance URI and derives the D4 publish pointers — the
    # job reads these verbatim (layout convention lives in train.py only).
    env = ray.posts[0]["runtime_env"]["env_vars"]
    assert json.loads(env["RASK_PARAM_FEATURES"]) == [{"dataset": "silver$features", "version": 7, "uri": f"{_ROOT}/medallion/silver"}]
    assert env["RASK_DEST_URI"] == f"{_ROOT}/medallion/models/churn"
    assert env["RASK_PARAM_ARTIFACT_BASE"] == f"{_ROOT}/medallion/model-artifacts/churn"

    _ray(monkeypatch, _Ray(unreachable=True))
    assert asyncio.run(train.handle_train_trigger(_settings(), _EVENT, dapr=_FakeDapr())) == {"status": "RETRY"}

    assert asyncio.run(train.handle_train_trigger(_settings(), {"data": {}}, dapr=_FakeDapr())) == {"status": "DROP"}


def test_consumer_drops_unpinned_or_empty_features(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review 2026-07-10: a version-less feature would train on floating LATEST (violates D1) and an
    # empty-after-filter list would gate vacuously — both are malformed triggers: DROP, never repair.
    ray = _ray(monkeypatch)
    unpinned = {"data": {"token": "t", "model": "m", "features": [{"dataset": "silver$features"}]}}
    assert asyncio.run(train.handle_train_trigger(_settings(), unpinned, dapr=_FakeDapr()))["status"] == "DROP"
    junk = {"data": {"token": "t", "model": "m", "features": ["junk"]}}
    assert asyncio.run(train.handle_train_trigger(_settings(), junk, dapr=_FakeDapr()))["status"] == "DROP"
    empty = {"data": {"token": "t", "model": "m", "features": []}}
    assert asyncio.run(train.handle_train_trigger(_settings(), empty, dapr=_FakeDapr()))["status"] == "DROP"
    assert ray.posts == []


def test_consumer_drops_path_unsafe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # #115b: model/token/dataset from the BUS become S3 key prefixes and Lance URIs — a traversal-shaped
    # or separator-carrying name is a malformed trigger, DROPped before any URI is derived.
    ray = _ray(monkeypatch)
    ok = {"dataset": "silver$features", "version": 7}
    for data in (
        {"token": "t1", "model": "../etc", "features": [ok]},
        {"token": "a/b", "model": "churn", "features": [ok]},
        # the keys `POST /train` refuses: the token is one path-safe segment, with no dot
        {"token": "my.retry.key", "model": "churn", "features": [ok]},
        {"token": "..", "model": "churn", "features": [ok]},
        {"token": "-leading-dash", "model": "churn", "features": [ok]},
        {"token": "t1", "model": "churn", "features": [{"dataset": "silver$../evil", "version": 1}]},
        {"token": "t1", "model": "churn", "features": [{"dataset": "a$b$c", "version": 1}]},
        # a BARE dataset name is rejected too: it would derive a wrong stage URI AND (in the job's
        # lineage) a namespace equal to the whole name, corrupting the shared graph node's namespace
        {"token": "t1", "model": "churn", "features": [{"dataset": "events", "version": 1}]},
    ):
        assert asyncio.run(train.handle_train_trigger(_settings(), {"data": data}, dapr=_FakeDapr())) == {"status": "DROP"}
    assert ray.posts == []


def test_consumer_drops_oversized_or_nondict_config_and_too_many_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2026-07-11: the head's claim-check bound must hold at the CONSUMER too — the bus is a
    # wider trust surface, and config flows verbatim into the Ray Jobs runtime_env.
    ray = _ray(monkeypatch)
    ok = {"dataset": "silver$features", "version": 7}
    huge = {"blob": "x" * (train._MAX_CONFIG_BYTES + 1)}
    for data in (
        {"token": "t1", "model": "churn", "features": [ok], "config": huge},
        {"token": "t1", "model": "churn", "features": [ok], "config": ["not-a-dict"]},
        {"token": "t1", "model": "churn", "features": [ok] * (train.MAX_FEATURES + 1)},
    ):
        assert asyncio.run(train.handle_train_trigger(_settings(), {"data": data}, dapr=_FakeDapr())) == {"status": "DROP"}
    assert ray.posts == []


def test_train_route_422s_the_names_its_consumer_would_drop() -> None:
    # Review 2026-07-11: the head refuses what the consumer would DROP — never a 202 into a silent
    # no-op. Pydantic pattern/max_length gates mirror the consumer's _safe_name/_safe_dataset/cap.
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: _settings()
    client = TestClient(app)
    ok_feature = {"dataset": "silver$features", "version": 1}
    # A VALID key on every post: the header is required, so without one each body is refused for the
    # missing key and the body checks below are never what answers.
    key = {"Idempotency-Key": "idem-test"}
    for body in (
        {"model": "../etc", "features": [ok_feature]},
        {"model": "churn", "features": [{"dataset": "events", "version": 1}]},  # bare name
        {"model": "churn", "features": [{"dataset": "silver$../evil", "version": 1}]},
        {"model": "churn", "features": [ok_feature] * (train.MAX_FEATURES + 1)},
    ):
        response = client.post("/train", json=body, headers=key)
        assert response.status_code == 422, response.text
        assert all(error["loc"][0] == "body" for error in response.json()["detail"]), response.text


#: `(Idempotency-Key, whether a training run follows)`. Every row is answered by BOTH the door and the
#: consumer; a row on which they disagree is a request its caller was told 202 that never trains.
_TRAINING_KEYS = [
    ("idem-test", True),
    ("a" * 64, True),
    ("my.retry.key", False),  # the token is one path-safe segment (`TOKEN_PATTERN`), and a segment has no dot
    ("-leading-dash", False),
    ("", False),
    ("a" * 65, False),
]


@pytest.mark.parametrize(("key", "trains"), _TRAINING_KEYS)
def test_the_door_202s_exactly_the_tokens_its_consumer_trains_on(monkeypatch: pytest.MonkeyPatch, key: str, trains: bool) -> None:
    """`POST /train` and `/train-trigger` read ONE token grammar.

    The door's `Idempotency-Key` becomes the trigger's `token`, and the consumer DROPs a token outside
    its shape. A door wider than its consumer therefore answers 202 to a run that never starts, and the
    caller holding that 202 has no way to learn it. The halves share one bus: the consumer is fed the
    trigger the door itself published, so a door that alters the key on its way to the trigger fails
    here too. A refused key must publish nothing.
    """

    _ray(monkeypatch)
    bus = _FakeDapr()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: bus
    app.dependency_overrides[get_settings] = lambda: _settings()

    door = TestClient(app).post("/train", json={"model": "churn", "features": [{"dataset": "silver$features", "version": 7}]}, headers={"Idempotency-Key": key})

    assert door.status_code == (202 if trains else 422), door.text
    if not trains:
        assert bus.published == [], "a refused request published a training trigger"
        return
    assert door.json()["token"] == key
    consumer = asyncio.run(train.handle_train_trigger(_settings(), {"data": json.loads(bus.published[0]["data"])}, dapr=_FakeDapr()))
    assert consumer["status"] == "SUCCESS"
    ((planned, _written),) = train_plans.plan_store(_settings()).open_entries()
    assert door.json()["instance_id"] == planned, "the door answered a run id the consumer did not plan under"


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
