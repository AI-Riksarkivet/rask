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


def test_ingest_row_reaches_the_ingest_plane_and_a_sibling_prefix_does_not_fall_into_it() -> None:
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

    # THE PROPERTY SURVIVES ITS EXAMPLE. `/api/ingest-iiif` was a real row through a deprecation
    # window; A12 deleted the medallion route it pointed at, so it resolved to a backend that 502'd
    # instead of a path that 404s — and the row is now gone. The sibling-prefix rule it demonstrated
    # is what must not regress: a `-` suffix must NOT fall into the `/api/ingest` row and be
    # rewritten into the ingest plane, which would turn a clean "no upstream" into a wrong-service
    # 404. Asserted on the absent row, because that is the direction the bug now runs.
    legacy = _pick_route("/api/ingest-iiif", rows)
    assert legacy is None, "a sibling prefix must not fall into the /api/ingest row — it has no upstream, and 404 is the correct answer"
