"""gateway routing — Dapr invoke base vs httpx fallback (no network)."""

import importlib

import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


def test_ray_rows_map_to_the_compute_service(gw) -> None:
    routes = gw._routes()
    picked = gw._pick_route("/api/ray/jobs", routes)
    assert picked is not None
    _prefix, upstream_prefix, app_id, fallback = picked
    assert app_id == "compute"  # R22: service is `compute`; the public /api/ray path names the Ray cluster
    assert upstream_prefix == "/api/ray"  # rask rows forward unrewritten
    assert fallback.endswith(":8804")
    # the Serve proxy rides the same service
    assert gw._pick_route("/api/serve/status", routes)[2] == "compute"


def test_media_objects_ride_the_viewer_row(gw) -> None:
    # The storage browser (R18): /api/media/objects → the media-plane viewer with
    # the /api/media prefix rewritten to the viewer's internal /api.
    routes = gw._routes()
    picked = gw._pick_route("/api/media/objects", routes)
    assert picked is not None
    prefix, upstream_prefix, app_id, _fallback = picked
    assert app_id == "viewer"
    assert prefix == "/api/media"
    assert upstream_prefix == "/api"


def test_no_catch_all_since_the_r6_r20_wave(gw) -> None:
    # core-api and its /api catch-all are gone: an unmatched /api/* path picks NO
    # route (the proxy then 404s with "no upstream") instead of silently riding
    # to a dead upstream.
    routes = gw._routes()
    assert gw._pick_route("/api/batches/", routes) is None
    assert gw._pick_route("/api/search/q", routes) is None
    assert gw._pick_route("/api/volumes/objects", routes) is None


def test_target_base_uses_sidecar_when_enabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    base = gw._target_base("compute", "http://127.0.0.1:8804")
    assert base == "http://127.0.0.1:3500/v1.0/invoke/compute/method"


def test_target_base_falls_back_when_disabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    base = gw._target_base("compute", "http://127.0.0.1:8804")
    assert base == "http://127.0.0.1:8804"
