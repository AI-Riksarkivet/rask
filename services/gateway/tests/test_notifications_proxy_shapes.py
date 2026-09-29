"""What actually crosses the `/api/notifications` row — method, body and query bytes.

`test_notifications_route.py` establishes the three `test_lance_routes.py` shapes: the row exists, it
rewrites, the upstream is env-overridable. All three drive GET with no query string, which leaves the
two things this particular row depends on unasserted:

* **The mutations are POSTs with bodies.** `onseen` and `ondismiss` are the seam the shared bell has
  carried since before it had a backend, and both are `POST … {json}`. A proxy verified only on GET
  can drop a body or downgrade a method and every read-path test stays green.
* **The cursor is opaque, and opaque means byte-identical.** It is base64url — `-` and `_` — and the
  client is told to hand it back unmodified. A re-encoding hop anywhere on this row turns a valid
  cursor into a 400 at the service, which surfaces as a feed that stops after page one.

The mock transport returns `stream=httpx.ByteStream(...)`: the proxy re-streams via `aiter_raw()`, and
a preloaded body is already marked consumed (`StreamConsumed`).
"""

import importlib

import httpx
import pytest
from fastapi.testclient import TestClient


# The cursor shape this row must not touch: base64url of an `(occurred_at, notification_id)` pair,
# padding stripped, so it carries the two alphabet characters a re-encoding hop would rewrite.
CURSOR = "eyJvY2N1cnJlZF9hdCI6IjIwMjYtMDgtMDlUMTI6MDA6MDBaIiwibm90aWZpY2F0aW9uX2lkIjoicnVuLTAwMUBGQUlMIn0-_"


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


@pytest.fixture
def proxied(gw):
    """(TestClient, captured upstream requests) with every upstream mocked 200."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


def test_the_seen_seam_reaches_the_upstream_as_a_post_with_its_body(proxied) -> None:
    """Method, path, body and content-type, together — the four a body-carrying hop can lose one at a
    time while the response still looks fine.

    Sent as raw `content=` rather than `json=` so the assertion is on the BYTES: a hop that re-encoded
    an equivalent body would satisfy a parsed comparison and still be a hop this row must not have.
    """
    client, captured = proxied
    body = b'{"notification_ids": ["run-001@FAIL", "run-002@FAIL"]}'

    response = client.post("/api/notifications/inbox/seen", content=body, headers={"content-type": "application/json"})

    assert response.status_code == 200
    forwarded = captured[-1]
    assert forwarded.method == "POST"
    assert str(forwarded.url) == "http://127.0.0.1:8850/api/notifications/inbox/seen"
    assert forwarded.read() == body
    assert forwarded.headers["content-type"] == "application/json"


def test_the_opaque_cursor_survives_the_hop_byte_for_byte(proxied) -> None:
    """The client is told to hand the string back, not to construct one — which only holds if nothing
    between the two ends rewrites it. `-` and `_` are the base64url characters a re-encode would turn
    into `+` and `/`, and the service refuses a cursor it did not mint."""
    client, captured = proxied

    client.get("/api/notifications/inbox", params={"state": "unread", "limit": 20, "cursor": CURSOR})

    url = captured[-1].url
    assert url.params["cursor"] == CURSOR
    assert f"cursor={CURSOR}".encode() in url.query, f"the cursor was re-encoded on the way through: {url.query!r}"
    assert url.params["state"] == "unread"
    assert url.params["limit"] == "20"
