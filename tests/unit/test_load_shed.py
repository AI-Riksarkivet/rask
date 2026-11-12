"""Load-shed middleware — caps the bulk table writes reading their bodies at once, shedding the overflow with 429 (P5).

A bulk write is known by its ROUTE, `POST /v1/table/<id>/{create,insert,merge_insert}`, never by its Content-Type: the
handlers buffer the body whatever the header says, so a write that names another media type, or none, buffers as much.
A write takes its seat when its body is first read, so one the catalog judges or refuses from its headers holds none.

Driven at the ASGI layer (not via TestClient, which is synchronous) so overlap is deterministic: one write is
held in flight while a second is issued, proving the cap sheds the second BEFORE its body is read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated

import pytest
from fastapi import Body, FastAPI, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from catalog.api.load_shed import WriteConcurrencyLimitMiddleware


_ARROW = b"application/vnd.apache.arrow.stream"


def _scope(method: str, path: str, content_type: bytes | None = None) -> Scope:
    headers = [(b"content-type", content_type)] if content_type else []
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("catalog", 80),
    }


def _write(path: str = "/v1/table/t/insert") -> Scope:
    """A bulk Arrow-IPC write — the thing the cap gates (POST + write suffix + Arrow content-type)."""
    return _scope("POST", path, _ARROW)


async def _drive(mw: WriteConcurrencyLimitMiddleware, scope: Scope) -> tuple[int, int]:
    """Run one request through the middleware: the HTTP status answered, and how many times its body was read."""
    status = 0
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"rows", "more_body": False}

    async def send(msg: Message) -> None:
        nonlocal status
        if msg["type"] == "http.response.start":
            status = int(msg["status"])

    await mw(scope, receive, send)
    return status, reads


async def _answer(send: Send, status: int) -> None:
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _reading_app(release: asyncio.Event, entered: asyncio.Event) -> ASGIApp:
    """An app that reads its body first, as every write handler does, and holds an insert until ``release`` is set."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        if str(scope["path"]).endswith("/insert"):
            entered.set()
            await release.wait()
        await _answer(send, 200)

    return app


def test_a_write_over_the_cap_is_shed_with_429_before_its_body_is_read() -> None:
    async def scenario() -> tuple[tuple[int, int], tuple[int, int]]:
        release, entered = asyncio.Event(), asyncio.Event()
        mw = WriteConcurrencyLimitMiddleware(_reading_app(release, entered), max_concurrent=1)
        seated = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()
        shed = await _drive(mw, _write("/v1/table/t/merge_insert"))
        release.set()
        return await seated, shed

    assert asyncio.run(scenario()) == ((200, 1), (429, 0))


def test_a_write_not_yet_reading_its_body_holds_no_seat() -> None:
    """A write the catalog is still judging from its headers, here one it then refuses, has read no body, so a write
    behind it is served rather than shed."""

    async def scenario() -> tuple[tuple[int, int], tuple[int, int]]:
        judging, judged = asyncio.Event(), asyncio.Event()

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if str(scope["path"]).endswith("/insert"):
                judging.set()
                await judged.wait()
                await _answer(send, 401)
                return
            await receive()
            await _answer(send, 200)

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        refused = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await judging.wait()
        served = await _drive(mw, _write("/v1/table/t/merge_insert"))
        judged.set()
        return await refused, served

    assert asyncio.run(scenario()) == ((401, 0), (200, 1))


def test_a_write_whose_declared_body_fastapi_reads_through_a_function_middleware_is_shed_429() -> None:
    """The catalog's writes declare their Arrow body, which FastAPI reads before the handler and answers its own 400 when
    the read raises, and pass the catalog's function middlewares: a shed decided in that read is still answered 429."""

    async def scenario() -> tuple[tuple[int, int], tuple[int, int]]:
        release, entered = asyncio.Event(), asyncio.Event()
        app = FastAPI()

        @app.middleware("http")
        async def _gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            return await call_next(request)

        @app.post("/v1/table/{id}/insert")
        async def _insert(id: str, data: Annotated[bytes, Body(media_type=_ARROW.decode())]) -> dict[str, int]:
            if id == "held":
                entered.set()
                await release.wait()
            return {"rows": len(data)}

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        seated = asyncio.create_task(_drive(mw, _write("/v1/table/held/insert")))
        await entered.wait()
        shed = await _drive(mw, _write("/v1/table/other/insert"))
        release.set()
        return await seated, shed

    assert asyncio.run(scenario()) == ((200, 1), (429, 0))


def test_metadata_creates_are_not_shed_even_at_capacity() -> None:
    # The `/create` suffix also ends CHEAP metadata endpoints (namespace / tags / version / materialized_view
    # create) that send JSON and buffer no Arrow body. They must pass through even while the cap is full: the route
    # is the bulk table write's alone, `/v1/table/<one id segment>/<action>`. (audit: loadshed-create-suffix)
    async def scenario() -> list[tuple[int, int]]:
        release, entered = asyncio.Event(), asyncio.Event()
        mw = WriteConcurrencyLimitMiddleware(_reading_app(release, entered), max_concurrent=1)
        seated = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()
        codes = [
            await _drive(mw, _scope("POST", "/v1/namespace/analytics/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/table/t/tags/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/materialized_view/mv/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/table/t/version/create", _ARROW)),
        ]
        release.set()
        await seated
        return codes

    assert asyncio.run(scenario()) == [(200, 1)] * 4


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param(_ARROW, id="arrow"),
        pytest.param(b"application/octet-stream", id="another-media-type"),
        pytest.param(None, id="no-content-type"),
    ],
)
def test_table_create_is_shed_whatever_its_content_type(content_type: bytes | None) -> None:
    # The counterpart: a table create buffers its body whatever its header names, so it IS gated by the cap.
    async def scenario() -> tuple[int, int]:
        release, entered = asyncio.Event(), asyncio.Event()
        mw = WriteConcurrencyLimitMiddleware(_reading_app(release, entered), max_concurrent=1)
        seated = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()
        shed = await _drive(mw, _scope("POST", "/v1/table/t2/create", content_type))
        release.set()
        await seated
        return shed

    assert asyncio.run(scenario()) == (429, 0)


def test_zero_disables_the_limit() -> None:
    mw = WriteConcurrencyLimitMiddleware(_reading_app(asyncio.Event(), asyncio.Event()), max_concurrent=0)

    assert asyncio.run(_drive(mw, _write("/v1/table/t/merge_insert"))) == (200, 1)
