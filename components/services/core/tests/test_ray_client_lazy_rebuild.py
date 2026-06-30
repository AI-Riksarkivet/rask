"""get_ray_client must lazily (re)build a Ray client when the cached one is None.

Root cause it guards against: the Ray client is built ONCE in the lifespan. A
freshly-provisioned project's core-api boots before its RayService head exists,
so build_client returns None — and without a rebuild, every /chunks/{id}/submit
returns 503 "Ray dashboard unreachable" until core-api is manually restarted.
"""

import pytest

from core.api import dependencies


class _Settings:
    def __init__(self, dash: str) -> None:
        self.ray_dashboard_url = dash


class _State:
    def __init__(self, dash: str) -> None:
        self.ray_client = None
        self.settings = _Settings(dash)


class _App:
    def __init__(self, dash: str) -> None:
        self.state = _State(dash)


class _Req:
    def __init__(self, dash: str) -> None:
        self.app = _App(dash)


def test_rebuilds_and_caches_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _Req("http://ray:8265")
    calls: list[str] = []
    sentinel = object()

    def fake_build(url: str) -> object:
        calls.append(url)
        return sentinel

    monkeypatch.setattr(dependencies, "build_ray_client", fake_build)
    got = dependencies.get_ray_client(req)
    assert got is sentinel
    assert req.app.state.ray_client is sentinel  # cached for next request
    assert calls == ["http://ray:8265"]


def test_returns_cached_without_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _Req("http://ray:8265")
    existing = object()
    req.app.state.ray_client = existing

    def fake_build(url: str) -> object:
        raise AssertionError("must not rebuild when a client is already cached")

    monkeypatch.setattr(dependencies, "build_ray_client", fake_build)
    assert dependencies.get_ray_client(req) is existing


def test_stays_none_when_dashboard_still_down(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _Req("http://ray:8265")
    monkeypatch.setattr(dependencies, "build_ray_client", lambda url: None)
    assert dependencies.get_ray_client(req) is None
    assert req.app.state.ray_client is None  # endpoint still 503s, but no crash
