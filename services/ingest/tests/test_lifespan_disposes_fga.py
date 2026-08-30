"""ING-11: the lifespan must close the OpenFGA client it opened.

`attach_auth` builds an aiohttp-backed `OpenFgaClient` onto `app.state.fga`, and an unclosed one is
collected with its session open — one half-open connection per replica against OpenFGA plus an
"Unclosed client session" on the way out. Every sibling governed service disposes it on shutdown; the
ingest lifespan shut down only the workflow runtime and leaked the client.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from ingest import _lifespan


class _StubFgaClient:
    """The one method disposal calls — records that it was awaited."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _StubRuntime:
    """Stand-in for the Dapr WorkflowRuntime so the lifespan neither reaches a sidecar nor hangs."""

    def start(self) -> None: ...

    def shutdown(self) -> None: ...


@pytest.mark.asyncio
async def test_lifespan_closes_the_fga_client_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubFgaClient()

    async def _fake_attach_auth(app: FastAPI, _settings: object, *, service: str, provision: bool) -> None:
        app.state.fga = stub

    async def _no_probe(*, capability: str) -> None: ...

    # Substitute the real client build so the test needs neither a reachable OpenFGA nor a store to
    # resolve, and neutralise the workflow-runtime/sidecar path — the leak under test is on the
    # shutdown side, and the runtime would otherwise block on a gRPC channel to a sidecar that is not
    # there.
    monkeypatch.setattr("ingest.attach_auth", _fake_attach_auth)
    monkeypatch.setattr("ingest.probe_actor_state_store", _no_probe)
    monkeypatch.setattr("dapr.ext.workflow.WorkflowRuntime", lambda: _StubRuntime())
    monkeypatch.setattr("ingest.workflow.register", lambda _runtime: None)

    app = FastAPI()
    app.state.fga = None
    app.state.oidc = None

    lifespan = _lifespan(None)
    async with lifespan(app):
        assert app.state.fga is stub

    assert stub.closed, "the ingest lifespan left app.state.fga open on shutdown"
