"""The lineage sidecar-only 403 guard (lance-ns-merge.md P1: the nginx
`lance.lineageSidecarOnlyRoutes` blocklist, ported to gateway middleware).

Dapr-delivered lineage routes (pub/sub ingest + the cron reconcile binding) must
never be reachable through the public edge — proxying them would ride the
gateway's own sidecar and stamp the trusted app-api-token. The nginx block was a
case-insensitive regex over the normalized URI; the middleware must cover the
same variants (trailing slash, sub-path, casing, `..`-normalization).
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
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # stream=, not json=/content=: the proxy re-streams via aiter_raw(), and a
        # preloaded body is already marked consumed (StreamConsumed).
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


@pytest.mark.parametrize(
    "path",
    [
        "/api/lineage/lineage-events",
        "/api/lineage/lineage-events/",
        "/api/lineage/lineage-events/sub",
        "/api/lineage/LINEAGE-EVENTS",  # nginx block was case-insensitive (~*)
        "/api/lineage/lineage-reconcile-cron",
        "/api/lineage/lineage-reconcile-cron/tick",
    ],
)
def test_sidecar_only_routes_403(gw, proxied, path: str) -> None:
    client, captured = proxied
    for method in ("get", "post"):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f"{method.upper()} {path} must be blocked at the edge"
    assert captured == [], "a blocked route must never reach an upstream"


def test_other_lineage_routes_pass_through(gw, proxied) -> None:
    client, captured = proxied
    resp = client.get("/api/lineage/runs")
    assert resp.status_code == 200
    assert str(captured[-1].url) == "http://127.0.0.1:8000/runs"


def test_blocklist_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart renders the one-source list into RASK_LINEAGE_SIDECAR_ONLY_ROUTES
    (e.g. a renamed reconcile binding); the middleware honours it.

    The override is applied BEFORE the app boots, which is the only moment it can arrive in a
    deployment — the chart writes it into the pod's environment. It used to be re-parsed on every
    request that reached this middleware, probes included; it is now read once, with the rest of the
    gateway's configuration, and served off `app.state.settings` (FLEET-ENV-SCATTER).
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_LINEAGE_SIDECAR_ONLY_ROUTES", "lineage-events,custom-cron")
    import gateway

    gw = importlib.reload(gateway)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert client.post("/api/lineage/custom-cron").status_code == 403
        assert client.get("/api/lineage/lineage-reconcile-cron").status_code == 200  # no longer listed
        assert str(captured[-1].url) == "http://127.0.0.1:8000/lineage-reconcile-cron"


def test_normalization_defeats_dot_segment_variants(gw) -> None:
    """Server-side canonicalization (clients like httpx normalize before sending,
    so the raw-socket variant is proven at the function seam nginx solved with
    `merge_slashes` + URI normalization)."""
    assert gw._normalize_path("/api/lineage/x/../lineage-events") == "/api/lineage/lineage-events"
    assert gw._normalize_path("/api/lineage/./lineage-events/") == "/api/lineage/lineage-events/"
    assert gw._normalize_path("//api//lineage//lineage-events") == "/api/lineage/lineage-events"
    assert gw._normalize_path("/../api/lineage/runs") == "/api/lineage/runs"
