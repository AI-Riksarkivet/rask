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


def test_ingest_row_reaches_the_ingest_plane_and_does_not_swallow_ingest_iiif() -> None:
    """The /api/ingest row, and the sibling-prefix property it only LOOKS like it violates.

    `_pick_route` matches on `path == prefix or path.startswith(prefix + "/")`, so "/api/ingest-iiif"
    cannot match the "/api/ingest" row — the next character is "-", not "/". Worth a test rather than
    a comment: the two rows coexist through a deprecation window, and "longest prefix first" is the
    kind of rule someone reorders on instinct.
    """
    from gateway import _pick_route, _routes

    rows = _routes()
    ingest = _pick_route("/api/ingest", rows)
    assert ingest is not None and ingest[2] == "ingest"

    sub = _pick_route("/api/ingest/ingests/abc", rows)
    assert sub is not None and sub[2] == "ingest"

    # The deprecated medallion head still resolves to the medallion, not to the new plane.
    legacy = _pick_route("/api/ingest-iiif", rows)
    assert legacy is not None and legacy[2] == "lance-ray"


def test_the_ingest_rewrite_lands_on_a_path_the_service_ACTUALLY_serves(monkeypatch) -> None:  # noqa: ANN001
    """The gateway's rewritten path must exist in the ingest app, not merely look plausible.

    This row shipped rewriting to "/v1", taken from the ingest module's own docstrings — which say
    `POST /v1/ingests`. That is the ROUTER's path, before the prefix `make_service_app` adds: the app
    mounts every router under `settings.api_prefix`, and the chart deploys `RASK_API_PREFIX=/api`
    (values.yaml:67, confirmed on the live pod), so the service serves `/api/ingests`. Every call
    through the gateway 404'd.

    The existing routing tests could not catch it, because they assert the gateway in ISOLATION: that
    a path picks the right row. A rewrite is only correct RELATIVE to what the other side serves.

    The prefix is pinned here rather than inherited, because the suite's own conftest sets
    `RASK_API_PREFIX=/api/v1` for config isolation — so an ambient reading would test a prefix no
    deployment uses, which is its own way of being wrong.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    from gateway import _pick_route, _routes
    from ingest import create_app

    served = set(create_app().openapi()["paths"])

    rows = _routes()
    for public, expected in (
        ("/api/ingest/ingests", "/api/ingests"),
        ("/api/ingest/ingests/{run_id}", "/api/ingests/{run_id}"),
    ):
        route = _pick_route(public, rows)
        assert route is not None, f"no gateway row for {public}"
        route_prefix, upstream_prefix, _, _ = route
        rewritten = upstream_prefix + public[len(route_prefix) :]

        assert rewritten == expected
        assert rewritten in served, f"the gateway rewrites {public} to {rewritten}, which the ingest app does not serve: {sorted(served)}"
