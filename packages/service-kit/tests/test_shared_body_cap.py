"""A body over the cap is refused before a handler buffers it, with the spec's problem body.

The catalog wraps its app in `BodySizeLimitMiddleware` (`catalog/main.py`), so a multi-GB Arrow-IPC write cannot reach
a buffer. The cap is a DoS ceiling, not a business rule: it stops a multi-GB body reaching a buffer, and a route that
wants a tighter bound states it itself.

The refusal is built by pure-ASGI middleware, which answers before the body is buffered and so cannot raise into the
problem handlers; it spells the spec's `INVALID_INPUT` itself, because `body_limit` stays importable without the
Lance-Namespace extra, and a generated client validating the body requires that `code`. Each test's app carries an
inner `http` middleware, as the catalog's read-only gate is: a `BaseHTTPMiddleware` whose task group wraps an app
exception in an `ExceptionGroup`, which the streaming refusal must still answer as a 413 rather than a 500.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode

from service_kit.body_limit import BodySizeLimitMiddleware


CAP = 1000


def _capped_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inner(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)

    @app.post("/echo")
    async def _echo(request: Request) -> dict[str, int]:
        return {"len": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=CAP)
    return app


def test_a_body_over_the_cap_is_refused_with_the_specs_problem_body() -> None:
    with TestClient(_capped_app()) as client:
        resp = client.post("/echo", content=b"x" * (CAP + 1))

    body = resp.json()
    assert (resp.status_code, resp.headers["content-type"]) == (413, "application/problem+json")
    assert (body["status"], body["code"], set(body)) == (413, int(ErrorCode.INVALID_INPUT), {"type", "title", "status", "detail", "code", "error"})


def test_a_body_at_the_cap_is_served() -> None:
    with TestClient(_capped_app()) as client:
        resp = client.post("/echo", content=b"x" * CAP)

    assert (resp.status_code, resp.json()) == (200, {"len": CAP})


def test_a_chunked_body_over_the_cap_is_refused_while_it_streams() -> None:
    """With no Content-Length the fast path cannot fire, so the streaming byte counter refuses it."""

    def stream() -> Iterator[bytes]:
        for _ in range(4):
            yield b"x" * 500

    with TestClient(_capped_app()) as client:
        resp = client.post("/echo", content=stream())

    assert (resp.status_code, resp.json()["code"]) == (413, int(ErrorCode.INVALID_INPUT))
