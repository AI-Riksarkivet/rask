"""The /api/projects gateway route must resolve to controlplane, before core."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing `gateway` builds the app (make_service_app -> build_settings).
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")


def test_projects_route_present_and_before_core() -> None:
    from gateway import _routes

    routes = _routes()
    prefixes = [r[0] for r in routes]
    assert "/api/projects" in prefixes
    assert prefixes.index("/api/projects") < prefixes.index("/api")


def test_projects_route_targets_controlplane() -> None:
    from gateway import _routes

    proj = next(r for r in _routes() if r[0] == "/api/projects")
    assert proj[2] == "controlplane"
