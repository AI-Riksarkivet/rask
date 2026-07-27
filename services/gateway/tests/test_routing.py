"""gateway routing — Dapr invoke base vs httpx fallback (no network)."""

import importlib

import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


def test_routes_map_prefix_to_appid(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    routes = gw._routes()
    picked = gw._pick_route("/api/search/q", routes)
    assert picked is not None
    _prefix, upstream_prefix, app_id, fallback = picked
    assert app_id == "search-api"
    assert upstream_prefix == "/api/search"  # rask rows forward unrewritten
    assert fallback.endswith(":8802")
    # catch-all → core-api
    assert gw._pick_route("/api/batches/", routes)[2] == "core-api"


def test_target_base_uses_sidecar_when_enabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    base = gw._target_base("core-api", "http://127.0.0.1:8801")
    assert base == "http://127.0.0.1:3500/v1.0/invoke/core-api/method"


def test_target_base_falls_back_when_disabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    base = gw._target_base("core-api", "http://127.0.0.1:8801")
    assert base == "http://127.0.0.1:8801"
