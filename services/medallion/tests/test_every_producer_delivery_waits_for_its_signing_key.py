"""Every sidecar-delivered route of the medallion producer is answered RETRY before it is read while its key is unresolved.

[[LH-064]]. Dapr delivers a message to a pod whatever its readiness says, so a producer that is waiting for its signing
key would otherwise start the cascade, hold a promotion and schedule a training watch from events it could not sign. A
DROP would lose the work and a malformed trigger is dropped today, so the answer has to come before the trigger is
parsed. The stage runner's one route is `test_a_signer_without_its_key_emits_nothing.py`; these are the producer's four,
each wired separately, so a route added without the gate is a delivery that is lost. The producer's own lifespan resolves
its key for readiness the way the stage runner's does, and that wiring is a second test here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineage_kit import SigningKey, parse_published_keys
from medallion.api.bronze_arrival import register_bronze_arrival_route
from medallion.api.promotions import register_promotion_route
from medallion.api.train import register_train_trigger_route
from medallion.core.config import get_settings
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing


@pytest.fixture
def control_lane(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The producer as the chart renders it with the control lane on, so `/publication-arrival` is registered."""
    monkeypatch.setenv("MEDALLION_CONTROL_PUBSUB", "catalog-control-pubsub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("/bronze-arrival", id="the-bronze-write-that-wakes-the-cascade"),
        pytest.param("/publication-arrival", id="a-publication-that-wakes-it"),
        pytest.param("/train-trigger", id="a-training-request"),
        pytest.param("/promotion-held", id="a-promotion-held-for-review"),
    ],
)
def test_a_producer_without_its_key_answers_retry_to_every_delivery_before_reading_it(control_lane: None, route: str) -> None:
    app = FastAPI()
    app.state.dapr = None
    attach_signing(
        app, SigningKeyHolder(identity="service-medallion-producer", store="lance-secrets", load_key=SigningKey.from_seed, parse_published=parse_published_keys)
    )
    wrapper = register_bronze_arrival_route(app)
    register_train_trigger_route(app, wrapper)
    register_promotion_route(app, wrapper)
    malformed: dict[str, Any] = {"data": {"token": ["not", "a", "trigger"]}}

    answered = TestClient(app).post(route, json=malformed)

    assert (answered.status_code, answered.json()) == (200, {"status": "RETRY"})


class _Sidecar:
    """What the producer does with its Dapr client at shutdown."""

    async def close(self) -> None:
        return None


def test_a_producer_without_its_key_is_not_ready(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> None:
    identity = "service-medallion-producer"
    secrets = "http://localhost:3500/v1.0/secrets/lance-secrets"
    env = {
        "MEDALLION_SECRETS_FROM_DAPR": "true",
        "RASK_SIGNING_IDENTITY": identity,
        "MEDALLION_FGA_SERVICE_IDENTITY": identity,
        "DAPR_HTTP_PORT": "3500",
        "APP_API_TOKEN": "the-estate-app-token",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from medallion import producer

    get_settings.cache_clear()
    monkeypatch.setattr(producer, "DaprClient", _Sidecar)
    monkeypatch.setattr(producer, "instrument_lance_if_available", lambda: None)
    monkeypatch.setattr(producer, "register_tasks", lambda _settings: None)
    with respx.mock:
        respx.get(f"{secrets}/lance").mock(return_value=httpx.Response(200, json={"minio-secret-key": "the-s3-secret"}))
        respx.get(f"{secrets}/signing-key-{identity}").mock(return_value=httpx.Response(500))
        respx.get(f"{secrets}/signing-public-{identity}").mock(return_value=httpx.Response(404))
        with TestClient(producer.app) as client:
            readiness, liveness = client.get("/readyz"), client.get("/livez")
    get_settings.cache_clear()

    assert (readiness.status_code, liveness.status_code) == (503, 200)
