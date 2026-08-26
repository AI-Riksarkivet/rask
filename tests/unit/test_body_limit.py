"""Body-size limit middleware — 413 on oversized bodies, via both Content-Length and chunked transfer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from service_kit.body_limit import BodySizeLimitMiddleware


class _PassThrough(BaseHTTPMiddleware):
    """A no-op BaseHTTPMiddleware — its anyio task group wraps app exceptions in an ExceptionGroup (like the
    shipped maintenance_middleware), so the streaming 413 is exercised in the REAL composition."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)


def _app(max_bytes: int) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()  # read the full body, like the Arrow-IPC write endpoints do
        return {"len": len(body)}

    @app.exception_handler(Exception)
    async def catch_all(request: Request, exc: Exception) -> JSONResponse:  # mirror main.py's 500 handler
        return JSONResponse(status_code=500, content={"detail": "internal"})

    # Order matches main.py: a BaseHTTPMiddleware inside, BodySizeLimitMiddleware added last (outermost).
    app.add_middleware(_PassThrough)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    return app


def test_body_under_limit_passes() -> None:
    with TestClient(_app(1000)) as client:
        response = client.post("/echo", content=b"x" * 500)
    assert response.status_code == 200
    assert response.json() == {"len": 500}


def test_body_exactly_at_limit_passes() -> None:
    # Boundary: == max is allowed (the reject is strictly > max).
    with TestClient(_app(1000)) as client:
        response = client.post("/echo", content=b"x" * 1000)
    assert response.status_code == 200
    assert response.json() == {"len": 1000}


def test_body_one_over_limit_rejected() -> None:
    with TestClient(_app(1000)) as client:
        response = client.post("/echo", content=b"x" * 1001)
    assert response.status_code == 413


def test_body_over_limit_declared_content_length_rejected() -> None:
    # httpx sets Content-Length for a bytes body — the fast-path reject fires before the body is read.
    with TestClient(_app(1000)) as client:
        response = client.post("/echo", content=b"x" * 2000)
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 413
    assert "directly to object storage" in response.json()["detail"]


def test_body_over_limit_chunked_without_content_length_rejected() -> None:
    # A generator body is sent chunked (no Content-Length), so the fast path can't fire — the streaming
    # byte counter must catch it and still return 413 (not a 500 from an unhandled exception).
    def stream() -> Iterator[bytes]:
        for _ in range(4):
            yield b"x" * 500  # 2000 bytes total, over the 1000 cap

    with TestClient(_app(1000)) as client:
        response = client.post("/echo", content=stream())
    assert response.status_code == 413  # NOT 500 — the ExceptionGroup from the inner BaseHTTPMiddleware
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 413
