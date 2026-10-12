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
    base = gw._target_base("controlplane", "http://127.0.0.1:8820")
    assert base == "http://127.0.0.1:3500/v1.0/invoke/controlplane/method"


def test_a_sibling_prefix_does_not_fall_into_a_shorter_row() -> None:
    """`_pick_route` matches on `path == prefix or path.startswith(prefix + "/")`, so a sibling prefix never
    falls into a shorter row. `/api/catalog` is a row; `/api/catalogue` belongs to no row: it has no upstream,
    and 404 is the correct answer. "Longest prefix first" is the kind of rule someone reorders on instinct,
    which is why this is a test and not a comment.
    """
    from gateway import _pick_route, _routes

    rows = _routes()
    catalog = _pick_route("/api/catalog/v1/namespace/list", rows)
    assert catalog is not None and catalog.app_id == "catalog"

    assert _pick_route("/api/catalogue", rows) is None, "a sibling prefix must not fall into the /api/catalog row"
