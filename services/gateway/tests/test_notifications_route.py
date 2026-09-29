"""The `/api/notifications` gateway row (landed 2026-08-10, `3e4463a0`).

The three shapes `test_lance_routes.py` established, applied to the notification plane: the row is
present and reachable, it rewrites to the right upstream, and the upstream is env-overridable. The
fourth assertion here is the one specific to this row — public prefix and upstream prefix are both
interpolated from `RASK_API_PREFIX`, so they cannot drift the moment that prefix is not `/api`.
"""

import importlib

import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


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
