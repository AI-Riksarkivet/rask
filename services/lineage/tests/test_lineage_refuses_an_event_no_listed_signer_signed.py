"""With signing enforced, lineage records a bus event only when a listed signer's key verifies it.

[[LH-064]] C1 (owner ruling 2026-10-02: Ed25519, keys in the store). The bus door authenticates the sidecar, not the
producer, so a pod holding the estate's app token could record provenance as anyone. Enforced, an unsigned event and
one whose signature does not verify are refused: acked so the sidecar never redelivers or parks them, and not recorded.
A public-key source lineage cannot read is an outage, so the delivery is retried rather than refused.

Driven through the registered `/lineage-events` route with the production wiring, FGA off so only the signature decides.
The signer's public keys are read through the sidecar's secret API, stood in for by respx at the address the Dapr
sidecar serves; the signatures are built by the root conftest's `EventSigner` from the wire format alone.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.testclient import TestClient


SIGNER = "service-bronze-to-silver"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Feed:
    """The repository surface the bus door reaches on a first delivery: it records, and has nothing to replay."""

    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def ingest_event(self, event: Any) -> None:
        self.recorded.append(event.run_id)

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


def _event() -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "facets": {"author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "bus", "name": "stage.silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _Feed]:
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "LINEAGE_SIGNERS": json.dumps([SIGNER, "service-catalog"]),
        "LINEAGE_DELEGATORS": json.dumps(["service-catalog"]),
    }.items():
        monkeypatch.setenv(key, value)

    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    feed = _Feed()
    app.state.repository = feed
    return TestClient(app), feed


@respx.mock
@pytest.mark.parametrize(
    ("case", "status", "recorded"),
    [
        pytest.param("unsigned", "SUCCESS", False, id="an-unsigned-event"),
        pytest.param("tampered", "SUCCESS", False, id="an-event-changed-after-its-signer-signed-it"),
        pytest.param("store-down", "RETRY", False, id="a-public-key-source-lineage-cannot-read"),
    ],
)
def test_lineage_records_a_bus_event_only_when_a_listed_signer_signed_it(
    door: tuple[TestClient, _Feed], event_signer: Any, respx_allows_unused_routes: None, case: str, status: str, recorded: bool
) -> None:
    client, feed = door
    signer = event_signer(SIGNER)
    published = respx.get(f"{SECRETS}/signing-public-{SIGNER}")
    published.mock(return_value=httpx.Response(500) if case == "store-down" else httpx.Response(200, json={"keys": signer.public}))
    event = _event() if case == "unsigned" else signer.sign(_event())
    if case == "tampered":
        event["outputs"] = [{"namespace": "gold", "name": "acme-gold$events"}]

    answered = client.post("/lineage-events", json={"data": event}, headers={"dapr-api-token": "the-estate-app-token"})

    assert answered.status_code == 200, answered.text
    assert answered.json() == {"status": status}, f"{case}: the door answered {answered.json()}"
    assert bool(feed.recorded) is recorded, f"{case}: recorded {feed.recorded}"
