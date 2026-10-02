"""A stage runner that cannot read its private signing key is not Ready and takes no delivery, so it emits nothing.

[[LH-064]] C7 (owner ruling 2026-10-02: Ed25519, keys in the store). An event that leaves a signer unsigned is refused
by lineage once signatures are required, and a refused bus event is acknowledged and gone: so a signer must never emit
without its key. Readiness alone cannot hold it back, because Dapr delivers pub/sub messages to a pod whatever its
readiness says, so every sidecar-delivered route answers RETRY until the key resolves.

Driven at `medallion.stage_runner:app` through its own lifespan. The sidecar's secret API is stood in for by respx:
the S3 bundle answers, the stage runner's own private key does not. The Dapr client the lifespan builds is a
stand-in that records what would have been published.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient


IDENTITY = "service-bronze-to-silver"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Sidecar:
    """What the stage runner does with its Dapr client: publish, and close at shutdown."""

    published: list[str] = []

    async def publish_event(self, pubsub_name: str, topic_name: str, data: bytes | str, **_kwargs: object) -> None:
        del pubsub_name
        _Sidecar.published.append(topic_name)

    async def close(self) -> None:
        return None


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, event_signer: Any, respx_allows_unused_routes: None) -> Iterator[TestClient]:
    env = {
        "MEDALLION_SECRETS_FROM_DAPR": "true",
        "RASK_SIGNING_IDENTITY": IDENTITY,
        "MEDALLION_FGA_SERVICE_IDENTITY": IDENTITY,
        "DAPR_HTTP_PORT": "3500",
        "APP_API_TOKEN": "the-estate-app-token",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from medallion import stage_runner
    from medallion.core.config import MedallionSettings, get_settings

    get_settings.cache_clear()
    settings = MedallionSettings()
    monkeypatch.setattr(stage_runner, "_settings", settings)
    monkeypatch.setattr(stage_runner, "get_settings", lambda: settings)
    monkeypatch.setattr(stage_runner, "DaprClient", _Sidecar)
    monkeypatch.setattr(stage_runner, "instrument_lance_if_available", lambda: None)
    _Sidecar.published = []
    with respx.mock:
        respx.get(f"{SECRETS}/lance").mock(return_value=httpx.Response(200, json={"minio-secret-key": "the-s3-secret"}))
        respx.get(f"{SECRETS}/signing-key-{IDENTITY}").mock(return_value=httpx.Response(500))
        respx.get(f"{SECRETS}/signing-public-{IDENTITY}").mock(return_value=httpx.Response(200, json={"keys": event_signer(IDENTITY).public}))
        with TestClient(stage_runner.app) as client:
            yield client
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("surface", "answer"),
    [
        pytest.param("readiness", 503, id="the-readiness-probe"),
        pytest.param("delivery", {"status": "RETRY"}, id="a-sidecar-delivered-trigger"),
    ],
)
def test_a_stage_runner_without_its_key_is_not_ready_and_retries_every_delivery(runner: TestClient, surface: str, answer: object) -> None:
    if surface == "readiness":
        answered = runner.get("/readyz")
        observed: object = answered.status_code
    else:
        cloud_event = {
            "specversion": "1.0",
            "id": "c7",
            "source": "medallion-producer",
            "type": "com.dapr.event.sent",
            "datacontenttype": "application/json",
            "data": {"token": ["not", "a", "trigger"]},
        }
        answered = runner.post("/medallion-event", json=cloud_event, headers={"dapr-api-token": "the-estate-app-token"})
        observed = answered.json()

    assert observed == answer, f"{surface}: {answered.status_code} {answered.text}"
    assert _Sidecar.published == [], f"a signer without its key published {_Sidecar.published}"
