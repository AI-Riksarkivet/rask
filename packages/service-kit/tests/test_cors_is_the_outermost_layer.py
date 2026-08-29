"""SK-22 — CORS was registered FIRST and so ran INNERMOST, under every other layer.

Starlette inserts each `add_middleware` at the front of the stack, so the LAST registration is the
outermost. `register_middleware` registered CORS first and the body cap last, which put CORS beneath
BodySizeLimit, Timing and RequestID — and beneath `SlashToleranceMiddleware`, which `make_service_app`
adds on top of all four.

The consequence is a browser-visible one. Only a response the ROUTER produced ever got an
`Access-Control-Allow-Origin`; every response manufactured above CORS went back bare. The body-limit
413 is the one that matters, because it is the answer to an ordinary too-large upload: the browser
blocked it as a cross-origin violation, so the client saw an opaque network failure instead of the
413 telling it to use the direct path.
"""

from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from service_kit import make_service_app
from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.config import Settings


_ORIGIN = "https://ui.example"


def _settings(**over: object) -> Settings:
    return Settings.model_validate({"RASK_CORS_ORIGINS": [_ORIGIN], "RASK_API_PREFIX": "/api", **over})


def _app(**over: object):
    router = APIRouter()

    @router.post("/echo")
    async def echo() -> dict[str, str]:
        return {"ok": "1"}

    return make_service_app(title="cors-order", routers=[router], settings=_settings(**over))


def test_cors_is_registered_outside_the_body_cap() -> None:
    """Registration order is innermost-first, so CORS must come AFTER the cap in the list."""
    classes = [m.cls for m in _app().user_middleware]
    assert CORSMiddleware in classes and BodySizeLimitMiddleware in classes
    assert classes.index(CORSMiddleware) < classes.index(BodySizeLimitMiddleware), "starlette prepends, so a lower index is OUTER — CORS sat under the cap"


def test_an_over_cap_rejection_is_readable_by_the_browser() -> None:
    with TestClient(_app(RASK_MAX_BODY_BYTES=16)) as client:
        response = client.post("/api/echo", content=b"x" * 64, headers={"Origin": _ORIGIN})
        assert response.status_code == 413
        assert response.headers.get("access-control-allow-origin") == _ORIGIN, f"the 413 is invisible to the caller that provoked it: {dict(response.headers)}"


def test_a_handled_response_keeps_its_cors_headers() -> None:
    with TestClient(_app()) as client:
        response = client.post("/api/echo", headers={"Origin": _ORIGIN})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == _ORIGIN
        # The ids the docstring promises to expose survive the reorder.
        assert "x-request-id" in response.headers
        assert "x-response-time" in response.headers


def test_a_preflight_is_still_answered() -> None:
    with TestClient(_app()) as client:
        response = client.options("/api/echo", headers={"Origin": _ORIGIN, "Access-Control-Request-Method": "POST"})
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == _ORIGIN
