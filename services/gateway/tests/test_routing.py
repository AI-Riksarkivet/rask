"""gateway routing — Dapr invoke base vs httpx fallback (no network)."""

import importlib

import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


def test_target_base_uses_sidecar_when_enabled(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    base = gw._target_base("compute", "http://127.0.0.1:8804")
    assert base == "http://127.0.0.1:3500/v1.0/invoke/compute/method"


def test_a_sibling_prefix_does_not_fall_into_a_shorter_row() -> None:
    """`_pick_route` matches on `path == prefix or path.startswith(prefix + "/")`, so a sibling prefix never
    falls into a shorter row. `/api/train` and `/api/trains` are two rows to two producer paths, and
    `/api/training` belongs to neither: it has no upstream, and 404 is the correct answer. "Longest prefix
    first" is the kind of rule someone reorders on instinct, which is why this is a test and not a comment.
    """
    from gateway import _pick_route, _routes

    rows = _routes()
    train = _pick_route("/api/train", rows)
    assert train is not None and train[1] == "/train"

    trains = _pick_route("/api/trains/abc", rows)
    assert trains is not None and trains[1] == "/trains"

    assert _pick_route("/api/training", rows) is None, "a sibling prefix must not fall into the /api/train row"
