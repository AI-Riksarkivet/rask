# packages/service-kit/tests/test_slash_tolerance.py
from fastapi import APIRouter
from fastapi.testclient import TestClient

from service_kit import make_service_app


def _client() -> TestClient:
    r = APIRouter()

    @r.get("/items/")          # canonical WITH trailing slash
    def items() -> dict:
        return {"ok": "items"}

    @r.get("/ping")            # canonical WITHOUT trailing slash
    def ping() -> dict:
        return {"ok": "ping"}

    @r.post("/items/{item_id}/submit")
    def submit(item_id: str) -> dict:
        return {"ok": item_id}

    app = make_service_app(title="t", routers=[r])
    # api_prefix defaults to /api/v1 in tests; resolve it from the app for robustness
    return TestClient(app, follow_redirects=False)


def test_missing_trailing_slash_served_not_redirected() -> None:
    c = _client()
    # Dapr drops the slash: /api/v1/items arrives though canonical is /items/
    resp = c.get("/api/v1/items")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "items"}


def test_extra_trailing_slash_served_not_redirected() -> None:
    c = _client()
    resp = c.get("/api/v1/ping/")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "ping"}


def test_canonical_paths_still_work() -> None:
    c = _client()
    assert c.get("/api/v1/items/").status_code == 200
    assert c.get("/api/v1/ping").status_code == 200


def test_parametrized_path_not_mangled() -> None:
    c = _client()
    resp = c.post("/api/v1/items/abc123/submit")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "abc123"}


def test_unknown_path_is_404_not_redirect() -> None:
    c = _client()
    assert c.get("/api/v1/nope").status_code == 404
