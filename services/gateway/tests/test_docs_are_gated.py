"""The front door's aggregated Swagger must not answer an anonymous caller.

open_fastapi-audit — "/docs and /openapi.json are on in production for every served app, and the
public front door serves an unauthenticated aggregated Swagger of the whole estate at /api/docs".

The gateway is the sharp end of that finding, and it needs its own test rather than the shared
constructor gate. These two paths are not FastAPI docs routes at all: they are branches inside the
`/api/{path:path}` catch-all, so `openapi_url=None` on the constructor does not touch them. They took
no dependency and checked no token, and `chart/templates/ingress.yaml` publishes `/api` at the edge —
so `GET https://<host>/api/openapi.json` returned the merged route table, parameter names and
request/response schemas of every backend the gateway fronts, to anyone.

It is also an amplification lever, which is the half easy to miss: one unauthenticated GET makes the
gateway fan out SEQUENTIALLY to every distinct upstream at a 10 s timeout each.

404, not 403 — the answer must be indistinguishable from a gateway that has no docs route, the same
authz-before-existence rule the promotion read follows.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gw_docs_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.delenv("RASK_DOCS", raising=False)
    import gateway

    return importlib.reload(gateway)


@pytest.fixture
def gw_docs_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_DOCS", "true")
    import gateway

    return importlib.reload(gateway)


@pytest.mark.parametrize("path", ["/api/openapi.json", "/api/docs"])
def test_the_aggregated_docs_are_CLOSED_by_default(gw_docs_off, path: str) -> None:
    """No flag set is exactly how the chart deploys the gateway today."""
    with TestClient(gw_docs_off.app) as client:
        response = client.get(path)
    assert response.status_code == 404, (
        f"{path} answered {response.status_code} with no RASK_DOCS set — this path is published at the "
        f"Ingress and returns every backend's schema to an unauthenticated caller"
    )


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_gateways_OWN_docs_routes_are_closed_by_default(gw_docs_off, path: str) -> None:
    """The constructor half. Separate from the catch-all because they fail independently — the
    branches above live inside `/api/{path:path}` and survive `openapi_url=None`."""
    with TestClient(gw_docs_off.app) as client:
        assert client.get(path).status_code == 404


def test_the_flag_actually_opens_them(gw_docs_on) -> None:
    """A gate that can only ever say no is not a flag, it is a deletion — and the audit asks for a
    flag, because internal consumers do use these in dev. This is the half that proves `RASK_DOCS`
    is wired to something rather than merely read."""
    with TestClient(gw_docs_on.app) as client:
        assert client.get("/api/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
