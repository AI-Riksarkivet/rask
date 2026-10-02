"""After a key rotation lineage still verifies an event signed with the identity's previous key, and no unpublished one.

[[LH-064]] C3. A rotation publishes the identity's new public key first and keeps the previous one, so an event signed
before the rotation (still in the 168 h stream, still staged in the outbox, or from a signer not yet restarted)
verifies. A key the identity's published list does not hold is refused once lineage has re-read the list, so an
unknown key id is never decided on a stale cache.

Driven through the registered `/lineage-events` route with signing enforced and FGA off. The published list comes from
the sidecar's secret API, stood in for by respx; signatures are built by the root conftest's `EventSigner`.
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


SIGNER = "service-maintenance"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Feed:
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
        "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d", "facets": {"author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "maintenance", "name": "compaction.acme-silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


@respx.mock
@pytest.mark.parametrize(
    ("signed_with", "recorded"),
    [
        pytest.param("previous", True, id="the-previous-key-after-a-rotation"),
        pytest.param("unpublished", False, id="a-key-the-published-list-does-not-hold"),
    ],
)
def test_lineage_verifies_the_previous_key_after_a_rotation_and_refuses_an_unpublished_one(
    monkeypatch: pytest.MonkeyPatch, event_signer: Any, respx_allows_unused_routes: None, signed_with: str, recorded: bool
) -> None:
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "LINEAGE_SIGNERS": json.dumps([SIGNER]),
        "LINEAGE_DELEGATORS": json.dumps([]),
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
    current, previous, unpublished = event_signer(SIGNER), event_signer(SIGNER), event_signer(SIGNER)
    respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(200, json={"keys": f"{current.public},{previous.public}"}))
    event = (previous if signed_with == "previous" else unpublished).sign(_event())

    answered = TestClient(app).post("/lineage-events", json={"data": event}, headers={"dapr-api-token": "the-estate-app-token"})

    assert answered.json() == {"status": "SUCCESS"}, f"{signed_with}: the door answered {answered.json()}"
    assert bool(feed.recorded) is recorded, f"{signed_with}: recorded {feed.recorded}"
