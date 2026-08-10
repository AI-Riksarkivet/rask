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


def test_the_dismiss_seam_carries_its_one_id(proxied) -> None:
    """Its own test rather than a parametrize case: dismissing keys on `run_id@STATE`, so the id is the
    payload, and a body dropped here would dismiss nothing while answering 200."""
    client, captured = proxied
    body = b'{"notification_id": "run-001@FAIL"}'

    client.post("/api/notifications/inbox/dismiss", content=body, headers={"content-type": "application/json"})

    assert captured[-1].read() == body


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


def _gateway_under(monkeypatch: pytest.MonkeyPatch):
    """The gateway module with the prefix pinned. Returned UNRELOADED — the caller reloads it, because
    a reload is what re-reads `RASK_API_PREFIX` into the route table and the caller is what decides
    when that happens relative to the other module."""
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return gateway


def _notifications_paths(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """What the notifications app actually serves under the SAME pinned prefix.

    `app` is a module-level singleton built at import, so the prefix has to be in the environment
    before the reload — asking the process's current app would answer about whichever suite imported
    it first, which under `--import-mode=importlib` is not a fixed order.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import notifications

    return set(importlib.reload(notifications).app.openapi()["paths"])


def test_the_rewritten_paths_are_paths_the_service_ACTUALLY_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway's rewrite, checked against the notifications app's OWN openapi.

    A rewrite is only correct RELATIVE to what the other side serves, and every other test on this row
    asserts the gateway in isolation: that a path picks the right upstream and is rewritten to a
    plausible-looking one. The ingest row shipped rewriting to `/v1` — taken from that service's own
    docstrings, which describe the ROUTER path before the prefix `make_service_app` adds — and every
    call through the gateway 404'd with nothing red anywhere.

    Both modules are reloaded under the pinned prefix because both bake it at import: the gateway
    builds its route table from `RASK_API_PREFIX`, and `notifications.app` is a module-level singleton
    that mounts its routers under the same variable.
    """
    routes = importlib.reload(_gateway_under(monkeypatch))._routes()
    served = _notifications_paths(monkeypatch)
    row = next(r for r in routes if r[2] == "notifications")

    public_paths = ("/api/notifications/inbox", "/api/notifications/inbox/unread", "/api/notifications/inbox/seen", "/api/notifications/inbox/dismiss")
    rewritten = {row[1] + public[len(row[0]) :] for public in public_paths}
    assert rewritten <= served, f"the gateway rewrites to paths this service does not serve: {sorted(rewritten - served)} (it serves {sorted(served)})"


def test_the_sidecar_delivery_route_is_not_reachable_through_this_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /lineage-events` is root-mounted, so no `/api/*` row can reach it — structurally, rather
    than by a blocklist.

    Lineage needed an explicit edge middleware for exactly this (`RASK_LINEAGE_SIDECAR_ONLY_ROUTES`)
    because its delivery routes sit UNDER its api prefix. This service's do not, and that is the
    property worth pinning: proxying a sidecar-delivery route would ride the gateway's own sidecar and
    stamp the trusted app-api-token onto an anonymous request — the measured bypass, arriving by a
    different door.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import notifications

    served = set(importlib.reload(notifications).app.openapi()["paths"])

    assert "/lineage-events" in served, "the delivery route moved — re-derive whether the edge can now reach it"
    assert not any(path.startswith("/api/") and "lineage-events" in path for path in served)


def test_a_neighbouring_prefix_is_not_swallowed_by_this_row(gw) -> None:
    """Prefix matching is segment-aware: `_pick_route` matches the prefix exactly or followed by `/`.
    A `startswith` written without that guard would route `/api/notificationsomething` here, which is a
    404 at this service reported as if the row owned it."""
    routes = gw._routes()

    assert gw._pick_route("/api/notifications", routes)[2] == "notifications"
    assert gw._pick_route("/api/notifications/inbox", routes)[2] == "notifications"
    assert gw._pick_route("/api/notificationsomething", routes) is None
