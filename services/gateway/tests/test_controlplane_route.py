"""The /api/projects gateway route must resolve to controlplane."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")


def test_projects_route_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import _routes

    routes = _routes()
    prefixes = [r[0] for r in routes]
    assert "/api/projects" in prefixes


def test_projects_route_targets_controlplane() -> None:
    from gateway import _routes

    proj = next(r for r in _routes() if r[0] == "/api/projects")
    assert proj[2] == "controlplane"
