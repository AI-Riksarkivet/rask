"""A client must not be able to assert a Dapr identity through the public edge.

Dapr stamps `dapr-caller-app-id` and `dapr-api-token` on the way IN to a backend, and the estate's
doors read them as proof of who is calling. daprd APPENDS its own stamp rather than replacing a
client's, and FastAPI's `Header()` binds the FIRST occurrence — so a client that sets the header
itself wins over the sidecar, and every caller-identity check downstream reads the caller's own claim.

Measured on the live cluster against the ingest door, AFTER that door had been fixed to refuse public
callers:

    anonymous POST, no header                        -> 403
    anonymous POST + `dapr-caller-app-id: gateway`   -> 403
    anonymous POST + `dapr-caller-app-id: medallion` -> 202 ACCEPTED

One forged header turned the fix back into the bypass it was written to close. The edge is the only
place the claim can be refused: a door sees one value and cannot tell whose it is, while the gateway
knows for a fact that anything on its public listener came from a client.
"""

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
def proxied(gw):
    """The gateway with a mock upstream that RECORDS what it was actually sent."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # stream=, not json=: the proxy re-streams via aiter_raw() and a preloaded body is already
        # marked consumed.
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


@pytest.mark.parametrize("header", ["dapr-caller-app-id", "dapr-api-token", "dapr-app-id", "x-lance-service-identity"])
def test_a_client_supplied_trust_header_never_reaches_the_upstream(proxied, header: str) -> None:
    """The load-bearing assertion, per header the estate treats as proof of identity.

    Asserted on what the UPSTREAM received rather than on the response, because the gateway forwards
    the request either way — a test that only checked the status code would pass with the header
    intact and the bypass wide open.
    """
    client, captured = proxied

    client.get("/api/catalog/v1/namespace/x/table/list", headers={header: "medallion"})

    assert captured, "the request never reached the upstream — the fixture is not exercising the proxy"
    assert header not in captured[-1].headers, (
        f"{header!r} was forwarded verbatim: a client can assert a Dapr identity and defeat every caller check downstream"
    )


def test_casing_does_not_smuggle_it_through(proxied) -> None:
    """HTTP header names are case-insensitive, and the strip compares lower-cased raw bytes.

    A strip written against the literal lower-case name would let `Dapr-Caller-App-Id` through, and
    nothing downstream would notice — httpx and Starlette both normalise on READ, so the forged value
    would bind exactly as the lower-case one does.
    """
    client, captured = proxied

    client.get("/api/catalog/v1/namespace/x/table/list", headers={"Dapr-Caller-App-ID": "medallion"})

    assert "dapr-caller-app-id" not in captured[-1].headers


def test_the_headers_the_estate_DOES_need_are_still_forwarded(proxied) -> None:
    """The strip must be surgical.

    `Authorization` is the whole user-bearer path — the fallback every door takes once the
    service-token path is refused for a public caller. Stripping it would close the bypass by making
    authenticated use impossible, which is not a fix. `Idempotency-Key` and `Content-Type` are
    likewise load-bearing for the ingest control API.
    """
    client, captured = proxied

    client.post(
        "/api/catalog/v1/namespace/x/table/list",
        headers={"Authorization": "Bearer abc", "Idempotency-Key": "key-1", "Content-Type": "application/json"},
        content=b"{}",
    )

    forwarded = captured[-1].headers
    assert forwarded.get("authorization") == "Bearer abc"
    assert forwarded.get("idempotency-key") == "key-1"
    assert forwarded.get("content-type") == "application/json"


def test_FastAPI_binds_the_FIRST_duplicate_which_is_why_the_strip_is_required(gw) -> None:
    """Pins the framework behaviour the whole defect rests on.

    If FastAPI ever bound the LAST occurrence, daprd's appended stamp would win and a client's forged
    value would be harmless. It binds the first, so the client's wins — and this test fails loudly if
    that ever changes, rather than leaving the strip looking like unexplained paranoia.
    """
    from typing import Annotated

    from fastapi import FastAPI, Header

    app = FastAPI()

    @app.get("/probe")
    def probe(dapr_caller_app_id: Annotated[str | None, Header()] = None) -> dict[str, str | None]:
        return {"bound": dapr_caller_app_id}

    with TestClient(app) as c:
        client_first = c.get("/probe", headers=[("dapr-caller-app-id", "medallion"), ("dapr-caller-app-id", "gateway")])
        daprd_first = c.get("/probe", headers=[("dapr-caller-app-id", "gateway"), ("dapr-caller-app-id", "medallion")])

    assert client_first.json()["bound"] == "medallion", "FastAPI no longer binds the first duplicate — re-derive the threat model"
    assert daprd_first.json()["bound"] == "gateway"
