# packages/service-kit/tests/test_slash_tolerance.py
from fastapi import APIRouter
from fastapi.testclient import TestClient

from service_kit import build_settings, make_service_app


def _client() -> tuple[TestClient, str]:
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
    prefix = build_settings().api_prefix.rstrip("/")
    return TestClient(app, follow_redirects=False), prefix


def test_missing_trailing_slash_served_not_redirected() -> None:
    c, p = _client()
    # Dapr drops the slash: <prefix>/items arrives though canonical is /items/
    resp = c.get(f"{p}/items")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "items"}


def test_extra_trailing_slash_served_not_redirected() -> None:
    c, p = _client()
    resp = c.get(f"{p}/ping/")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "ping"}


def test_canonical_paths_still_work() -> None:
    c, p = _client()
    assert c.get(f"{p}/items/").status_code == 200
    assert c.get(f"{p}/ping").status_code == 200


def test_parametrized_path_not_mangled() -> None:
    c, p = _client()
    resp = c.post(f"{p}/items/abc123/submit")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": "abc123"}


def test_unknown_path_is_404_not_redirect() -> None:
    c, p = _client()
    assert c.get(f"{p}/nope").status_code == 404
