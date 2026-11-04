"""Every app the factory builds refuses a body over its cap before a handler buffers it, with the spec's problem body.

`BodySizeLimitMiddleware` is registered by `service_kit.middleware.register_middleware` (owner ruling 2026-08-26), so a
service cannot be added without a cap and a route cannot arrive uncapped. The cap is a DoS ceiling, not a business
rule: it stops a multi-GB body reaching a buffer, and a route that wants a tighter bound states it itself.

The refusal is built by pure-ASGI middleware, which answers before the body is buffered and so cannot raise into the
problem handlers; it spells the spec's `INVALID_INPUT` itself, because `body_limit` stays importable without the
Lance-Namespace extra, and a generated client validating the body requires that `code`. Each test drives the app the
factory builds, whose `TimingMiddleware` is a `BaseHTTPMiddleware`: its task group wraps an app exception in an
`ExceptionGroup`, which the streaming refusal must still answer as a 413 rather than a 500.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode

from service_kit import make_service_app
from service_kit.config import Settings


CAP = 1000


def _capped_app() -> FastAPI:
    app = make_service_app(title="capped", routers=[], settings=Settings(max_body_bytes=CAP))

    @app.post("/echo")
    async def _echo(request: Request) -> dict[str, int]:
        return {"len": len(await request.body())}

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


def test_an_ordinary_body_is_served_under_the_default_cap() -> None:
    """The guard that stops the ceiling being tightened into a business rule."""
    app = make_service_app(title="default-cap", routers=[])

    @app.post("/echo")
    async def _echo(request: Request) -> dict[str, int]:
        return {"len": len(await request.body())}

    with TestClient(app) as client:
        resp = client.post("/echo", content=b"x" * 1024)

    assert (resp.status_code, resp.json()) == (200, {"len": 1024})
