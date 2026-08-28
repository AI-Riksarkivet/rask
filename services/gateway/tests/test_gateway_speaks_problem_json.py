"""The gateway's own errors answer RFC 9457, like every other service in the fleet.

open_python-audit `GW-NO-PROBLEM-JSON` (med). The gateway builds its own FastAPI and never installs
`register_handlers`, so its errors (`404 no upstream`, `502 upstream unreachable`, `400 bad path`)
render as FastAPI's default `{"detail": ...}` with `application/json`, while every proxied error from
a real service arrives as `application/problem+json`. One client error path therefore sees two body
shapes for one gateway depending on whether the gateway or an upstream produced the error.

The gateway is NOT a lance service, so it matches the FLEET taxonomy (`service_kit.exceptions`):
four keys, `type` in the `about:blank#` namespace, no Lance numeric `code`. Proxied 502 bodies from
lance services still carry their own richer envelope untouched — this only shapes the errors the
gateway itself raises.
"""

from __future__ import annotations

import importlib

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


def _client(gw, *, unreachable: bool = False) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if unreachable:
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json"})

    client = TestClient(gw.app, raise_server_exceptions=False)
    client.__enter__()
    gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _assert_problem(response: httpx.Response, status: int) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json"), f"not problem+json: {response.headers.get('content-type')}"
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail"}, f"not an RFC 9457 body: {sorted(body)}"
    assert body["status"] == status
    assert body["type"].startswith("about:blank#")


def test_a_404_no_upstream_is_problem_json(gw) -> None:
    _assert_problem(_client(gw).get("/api/does-not-exist"), 404)


def test_a_502_upstream_unreachable_is_problem_json(gw) -> None:
    _assert_problem(_client(gw, unreachable=True).get("/api/catalog/v1/x"), 502)


def test_the_sidecar_guard_403_is_problem_json(gw) -> None:
    """The gateway's OWN 403 (a sidecar-only lineage route), which was a hand-built `{"detail":…}`
    JSONResponse rather than problem+json — fixed in the same change."""
    routes = gw._lineage_sidecar_only_routes()
    if not routes:
        pytest.skip("no sidecar-only lineage routes configured in this build")
    _assert_problem(_client(gw).get(f"/api/lineage/{next(iter(routes))}"), 403)
