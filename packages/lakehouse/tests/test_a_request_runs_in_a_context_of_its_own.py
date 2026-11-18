"""A request runs in a context of its own, whatever its connection served before it (ADR 0209).

A server starts a connection's next request from wherever it finished the last: a request whose bytes arrived before the
previous answer ended is started by the task that answered it, in a copy of that task's context. Left so, the request
runs inside the previous request's span, so it takes that request's trace id and its own ``traceparent`` is never read,
and every context variable the previous door set is still set. A connection is the platform Gateway's, reused across
callers, so the previous request is another caller's.

Driven on a live uvicorn over one raw connection: two requests written before either is answered, the second naming the
caller's ``traceparent``.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import uvicorn
from fastapi import FastAPI, Request

from lakehouse.context import current_trace_id
from lakehouse.lance_app import build_lance_service_app
from lakehouse.lifecycle import mark_started
from lakehouse.schemas.health import Readiness, ReadinessStatus


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_UNTRACED = b"GET /seen HTTP/1.1\r\nHost: lakehouse\r\n\r\n"
_TRACED = f"GET /seen HTTP/1.1\r\nHost: lakehouse\r\ntraceparent: 00-{TRACE_ID}-00f067aa0ba902b7-01\r\n\r\n".encode()
#: What a door left behind for whoever runs in its context next.
_LEFT: ContextVar[str] = ContextVar("left_by_a_door", default="nothing")


async def _ready(_request: Request) -> Readiness:
    return Readiness(status=ReadinessStatus.ready)


def _app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        mark_started(app)
        yield

    app = build_lance_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=lifespan,
        log=logging.getLogger("t"),
        ready_check=_ready,
        max_body_bytes=1024,
        request_deadline_seconds=30,
    )

    @app.get("/seen")
    async def _seen() -> dict[str, str | None]:
        found = _LEFT.get()
        _LEFT.set("another request's")
        return {"found": found, "trace": current_trace_id()}

    return app


@contextmanager
def _served(app: FastAPI) -> Generator[int]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _answers(connection: socket.socket, count: int) -> list[dict[str, str | None]]:
    """The JSON bodies of ``count`` answers read off ``connection``, each framed by its ``content-length``."""
    buffer, bodies = b"", []
    while len(bodies) < count:
        head, separator, rest = buffer.partition(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")), None)
        if not separator or length is None or len(rest) < length:
            received = connection.recv(65536)
            assert received, f"the connection closed after {len(bodies)} answers"
            buffer += received
            continue
        bodies.append(json.loads(rest[:length]))
        buffer = rest[length:]
    return bodies


def test_a_request_sent_before_the_last_was_answered_runs_in_its_own_trace_and_context() -> None:
    with _served(_app()) as port, socket.create_connection(("127.0.0.1", port), timeout=30) as connection:
        connection.sendall(_UNTRACED + _TRACED)
        first, second = _answers(connection, 2)

    assert (first["found"], first["trace"] != TRACE_ID) == ("nothing", True), first
    assert second == {"found": "nothing", "trace": TRACE_ID}
