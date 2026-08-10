"""The `/api/notifications` gateway row (open_notifications.md S1).

The three shapes `test_lance_routes.py` established, applied to the notification plane: the row is
present and reachable, it rewrites to the right upstream, and the upstream is env-overridable. The
fourth assertion here is the one specific to this row — public prefix and upstream prefix are both
interpolated from `RASK_API_PREFIX`, so they cannot drift the moment that prefix is not `/api`.
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
    """(TestClient, captured upstream requests) with every upstream mocked 200."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # stream=, not json=/content=: the proxy re-streams via aiter_raw(), and a preloaded body is
        # already marked consumed (StreamConsumed).
        captured.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


def test_notifications_row_present_and_reachable(gw) -> None:
    prefixes = [r[0] for r in gw._routes()]
    assert "/api/notifications" in prefixes
    # There is no bare /api catch-all (it died with core-api in the R6/R20 wave) and no other row is a
    # prefix of this one — so nothing ahead of it in the list can swallow an inbox call. Asserted
    # through _pick_route rather than by eyeballing the order, because ORDER is not the property:
    # "the first matching prefix is this row" is.
    assert "/api" not in prefixes
    for path in ("/api/notifications", "/api/notifications/inbox", "/api/notifications/inbox/unread"):
        picked = gw._pick_route(path, gw._routes())
        assert picked is not None, f"{path} has no upstream"
        assert picked[2] == "notifications"


@pytest.mark.parametrize(
    ("public", "upstream"),
    [
        # The service mounts its own routers under RASK_API_PREFIX (make_service_app), so this row
        # rewrites to ITSELF — unlike the lance rows, which strip the public prefix whole.
        ("/api/notifications/inbox", "http://127.0.0.1:8850/api/notifications/inbox"),
        ("/api/notifications/inbox/unread", "http://127.0.0.1:8850/api/notifications/inbox/unread"),
        ("/api/notifications/inbox/seen", "http://127.0.0.1:8850/api/notifications/inbox/seen"),
    ],
)
def test_notifications_row_forwards_unrewritten(gw, proxied, public: str, upstream: str) -> None:
    client, captured = proxied
    resp = client.get(public)
    assert resp.status_code == 200
    assert str(captured[-1].url) == upstream


def test_notifications_upstream_env_overridable(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_NOTIFICATIONS_URL", "http://notifications.test:9850")
    row = next(r for r in gw._routes() if r[0] == "/api/notifications")
    assert row[3] == "http://notifications.test:9850"


def test_public_and_upstream_prefixes_track_the_api_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a non-default RASK_API_PREFIX both halves of the row move together.

    This is the whole reason the row is interpolated rather than the literal pair `("/api/notifications",
    "/api/notifications")`: that literal is indistinguishable from this one at the chart's prefix and
    silently wrong at any other, forwarding /api/v1/notifications/... to a path the service does not
    serve — a 404 that names the path rather than the config.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    import gateway

    routes = importlib.reload(gateway)._routes()
    row = next(r for r in routes if r[2] == "notifications")
    assert row[0] == "/api/v1/notifications"
    assert row[1] == "/api/v1/notifications"
