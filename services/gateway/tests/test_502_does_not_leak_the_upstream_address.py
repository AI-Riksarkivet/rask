"""The public 502 must not name the internal upstream address.

docs/DECISIONS.md "The Python estate audit" `GW-502-LEAKS-INTERNAL-ADDRESS` (med). When an upstream is unreachable the gateway
raised `HTTPException(502, f"upstream {base} unreachable: {exc}")` — and `base` is the INTERNAL
target (`http://127.0.0.1:8804`, or a Dapr `/v1.0/invoke/<app>/…` URL), while `exc` (httpx's own
error) typically repeats it. That body is returned to the public caller, at the edge the chart
publishes `/api` on. It is the exact leak `_rewrite_location` exists to scrub from redirect
`Location` headers — one function away, and undone here.

The caller learns nothing actionable from the internal host anyway: what it can act on is which
PUBLIC route failed and that the failure is upstream (502). The address, the port and the exception
detail belong in the gateway's own ERROR log, where an operator reads them.
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


@pytest.fixture
def unreachable(gw):
    """Every upstream refuses the connection, the way a crashed/not-started backend does."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with TestClient(gw.app, raise_server_exceptions=False) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client


def test_a_502_body_names_no_internal_address(unreachable) -> None:
    response = unreachable.get("/api/catalog/v1/namespaces")
    assert response.status_code == 502
    body = response.text
    assert "127.0.0.1" not in body, f"the 502 leaked the internal upstream address to the caller: {body!r}"
    assert "8804" not in body and ":8" not in body, f"the 502 leaked an internal port: {body!r}"
    assert "/v1.0/invoke/" not in body, f"the 502 leaked the Dapr invoke URL: {body!r}"


def test_the_502_still_says_which_PUBLIC_route_failed_and_that_it_is_upstream(unreachable) -> None:
    """Scrubbing the address must not blind the caller: it still gets a 502 (upstream, not the
    gateway) and the public prefix it asked for — what it can actually act on."""
    response = unreachable.get("/api/catalog/v1/namespaces")
    assert response.status_code == 502
    assert "upstream" in response.text.lower()
    assert "/api/catalog" in response.text, "the caller cannot tell which route failed"


def test_the_internal_detail_is_logged_for_the_operator(unreachable, caplog: pytest.LogCaptureFixture) -> None:
    """The address/port/exception the caller no longer sees must still reach the gateway's own log."""
    import logging

    with caplog.at_level(logging.ERROR, logger="gateway"):
        unreachable.get("/api/catalog/v1/namespaces")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "8804" in logged or "127.0.0.1" in logged, "the internal upstream detail was scrubbed from the caller AND the log — an operator cannot diagnose it"
