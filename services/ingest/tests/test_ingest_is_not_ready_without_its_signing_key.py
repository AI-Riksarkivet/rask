"""Ingest reports itself ready only while its signing key resolves and is listed ([[LH-064]]).

A staged lineage event is refused and deleted unless it is signed, so ingest's recovery path is only as good as its key.
The pod therefore leaves its Service while the key is unresolved (and `/livez` stays 200, so it is waited on rather
than restarted), and is ready again the moment the key is listed. Driven through the real app and its real lifespan,
with the workflow engine and the authorization client stood in: the claim is the wiring from the settings through the
holder to the probe, which a test of the holder alone cannot see.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import ingest


IDENTITY = "service-ingest"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _StubRuntime:
    """Stand-in for the Dapr WorkflowRuntime so the lifespan neither reaches a sidecar nor hangs."""

    def start(self) -> None: ...

    def shutdown(self) -> None: ...


@pytest.fixture
def ingest_app(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> Iterator[Any]:
    async def _no_auth(app: Any, _settings: object, *, service: str, provision: bool) -> None:
        app.state.fga = None
        app.state.oidc = None

    async def _no_probe(*, capability: str) -> None: ...

    monkeypatch.setattr("ingest.attach_auth", _no_auth)
    monkeypatch.setattr("ingest.probe_actor_state_store", _no_probe)
    monkeypatch.setattr("dapr.ext.workflow.WorkflowRuntime", lambda: _StubRuntime())
    monkeypatch.setattr("ingest.workflow.register", lambda _runtime: None)
    monkeypatch.setenv("RASK_INGEST_SECRETS_FROM_DAPR", "true")
    monkeypatch.setenv("RASK_SIGNING_IDENTITY", IDENTITY)
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    with respx.mock:
        yield ingest.create_app()


@pytest.mark.parametrize(
    ("answered", "status"),
    [
        pytest.param(True, 200, id="a-key-its-identity-lists"),
        pytest.param(False, 503, id="a-key-the-store-will-not-give"),
    ],
)
def test_ingest_is_ready_only_while_its_signing_key_is_listed(ingest_app: Any, event_signer: Any, answered: bool, status: int) -> None:
    pair = event_signer(IDENTITY)
    seed = httpx.Response(200, json={"seed": pair.seed}) if answered else httpx.Response(500)
    respx.get(f"{SECRETS}/signing-key-{IDENTITY}").mock(return_value=seed)
    respx.get(f"{SECRETS}/signing-public-{IDENTITY}").mock(return_value=httpx.Response(200, json={"keys": pair.public}))

    with TestClient(ingest_app) as client:
        readiness, liveness = client.get("/readyz"), client.get("/livez")

    assert (readiness.status_code, liveness.status_code) == (status, 200)
