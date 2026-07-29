"""Load-shed middleware — caps concurrent Arrow-IPC writes, shedding the overflow with 429 (P5).

Driven at the ASGI layer (not via TestClient, which is synchronous) so overlap is deterministic: one write is
held in flight while a second is issued, proving the cap sheds the second BEFORE its body is buffered.
"""

from __future__ import annotations

import asyncio

from catalog.api.load_shed import WriteConcurrencyLimitMiddleware
from starlette.types import Message, Receive, Scope, Send


_ARROW = b"application/vnd.apache.arrow.stream"


def _scope(method: str, path: str, content_type: bytes | None = None) -> Scope:
    headers = [(b"content-type", content_type)] if content_type else []
    return {"type": "http", "method": method, "path": path, "headers": headers}


def _write(path: str = "/v1/table/t/insert") -> Scope:
    """A bulk Arrow-IPC write — the thing the cap gates (POST + write suffix + Arrow content-type)."""
    return _scope("POST", path, _ARROW)


async def _drive(mw: WriteConcurrencyLimitMiddleware, scope: Scope) -> int:
    """Run one request through the middleware; return the HTTP status the middleware/app emitted."""
    status = 0

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: Message) -> None:
        nonlocal status
        if msg["type"] == "http.response.start":
            status = int(msg["status"])

    await mw(scope, receive, send)
    return status


def test_write_over_cap_is_shed_with_429_before_the_body() -> None:
    async def scenario() -> tuple[int, int, int]:
        release = asyncio.Event()
        entered = asyncio.Event()
        calls = 0

        async def app(_scope_: Scope, _receive: Receive, send: Send) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()  # hold the one slot in flight
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        # request 1 acquires the only slot and blocks inside app
        t1 = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()  # inflight is now 1
        # request 2 finds the cap full → 429, and the app is NOT entered a second time (shed before body)
        code2 = await _drive(mw, _write("/v1/table/t/merge_insert"))
        release.set()
        code1 = await t1
        return code1, code2, calls

    code1, code2, calls = asyncio.run(scenario())
    assert code2 == 429, "the write over the concurrency cap must be shed"
    assert code1 == 200, "the in-flight write completes normally"
    assert calls == 1, "the shed request must NOT reach the app (shed before the body is buffered)"


def test_reads_and_non_bulk_writes_are_not_gated() -> None:
    # A read (GET) and /commit (a small metadata op) must pass through even while a write slot is held.
    async def scenario() -> tuple[int, int]:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def app(scope: Scope, _receive: Receive, send: Send) -> None:
            if str(scope["path"]).endswith("/insert"):
                entered.set()
                await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        t1 = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()  # the one write slot is held in flight
        get_code = await _drive(mw, _scope("GET", "/v1/table/t"))
        commit_code = await _drive(mw, _scope("POST", "/v1/table/t/commit"))
        release.set()
        await t1
        return get_code, commit_code

    get_code, commit_code = asyncio.run(scenario())
    assert get_code == 200 and commit_code == 200, "reads + non-bulk writes must never be shed"


def test_metadata_creates_are_not_shed_even_at_capacity() -> None:
    # The `/create` suffix also ends CHEAP metadata endpoints (namespace / tags / version / materialized_view
    # create) that send JSON and buffer no Arrow body. They must pass through even while the cap is full — the
    # discriminator is the Arrow-IPC content-type, not the path suffix. (audit: loadshed-create-suffix)
    async def scenario() -> list[int]:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def app(scope: Scope, _receive: Receive, send: Send) -> None:
            if str(scope["path"]).endswith("/insert"):
                entered.set()
                await release.wait()  # hold the only slot
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        t1 = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()  # the one slot is held by a real Arrow write
        # These all end in a write suffix but carry JSON (or no) body → must NOT be shed.
        codes = [
            await _drive(mw, _scope("POST", "/v1/namespace/analytics/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/table/t/tags/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/materialized_view/mv/create", b"application/json")),
            await _drive(mw, _scope("POST", "/v1/table/t/create")),  # no content-type at all
        ]
        release.set()
        await t1
        return codes

    assert asyncio.run(scenario()) == [200, 200, 200, 200]


def test_table_create_with_arrow_body_is_shed() -> None:
    # The counterpart: a genuine table create DOES carry an Arrow stream, so it IS gated by the cap.
    async def scenario() -> int:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def app(_scope_: Scope, _receive: Receive, send: Send) -> None:
            entered.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=1)
        t1 = asyncio.create_task(_drive(mw, _write("/v1/table/t/insert")))
        await entered.wait()
        code = await _drive(mw, _write("/v1/table/t2/create"))  # Arrow create over the cap → shed
        release.set()
        await t1
        return code

    assert asyncio.run(scenario()) == 429


def test_zero_disables_the_limit() -> None:
    async def scenario() -> int:
        async def app(_scope_: Scope, _receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = WriteConcurrencyLimitMiddleware(app, max_concurrent=0)  # 0 => disabled
        return await _drive(mw, _write("/v1/table/t/insert"))

    assert asyncio.run(scenario()) == 200
