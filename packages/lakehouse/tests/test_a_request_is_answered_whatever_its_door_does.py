"""A lakehouse app answers every request with the spec's problem body, whatever its door does (ADR 0203).

`build_lance_service_app` installs two layers inside the request-id layer for catalog, lineage and maintenance. A door
that has begun no answer within the request deadline is answered 503 (code 17) and runs on to its own end, never
cancelled, since a lakehouse door undoes its half-done work in its own ``except`` blocks. A door in which a native library
panics, which pyo3 raises as a BaseException that no Exception handler catches, is answered 500 (code 18) as any other
failure is, rather than by the server's bare text 500.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lakehouse.lance_app import build_lance_service_app
from lakehouse.lifecycle import mark_started
from lakehouse.schemas.health import Readiness, ReadinessStatus


#: How long the door below runs past the deadline: long enough that an answer it gave would come after it.
_DOOR_SECONDS = 1.0


class _Panic(BaseException):
    """Where ``pyo3_runtime.PanicException`` sits: a BaseException that is no Exception and no request to stop."""


async def _ready(_request: Request) -> Readiness:
    return Readiness(status=ReadinessStatus.ready)


def _app(*, deadline_seconds: float) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        mark_started(app)
        yield

    return build_lance_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=False,
        lifespan=lifespan,
        log=logging.getLogger("t"),
        ready_check=_ready,
        max_body_bytes=1024,
        request_deadline_seconds=deadline_seconds,
    )


def test_a_door_past_its_deadline_is_answered_503_and_runs_on_to_its_end() -> None:
    app = _app(deadline_seconds=0.1)
    ended = threading.Event()

    @app.post("/slow")
    async def _slow() -> dict[str, str]:
        await asyncio.sleep(_DOOR_SECONDS)
        ended.set()
        return {"answer": "late"}

    with TestClient(app) as client:
        answered = client.post("/slow")
        ended_before_the_answer = ended.is_set()
        ran_to_its_end = ended.wait(_DOOR_SECONDS * 5)

    assert (answered.status_code, answered.headers["content-type"], answered.json()["code"]) == (503, "application/problem+json", 17), answered.text
    assert (ended_before_the_answer, ran_to_its_end) == (False, True), "the door was not left to run on to its end once its caller was answered"


def test_a_door_in_which_a_native_library_panics_is_answered_500_with_the_problem_body() -> None:
    app = _app(deadline_seconds=30.0)

    @app.get("/panics")
    async def _panics() -> None:
        raise _Panic("called `Option::unwrap()` on a `None` value")

    with TestClient(app, raise_server_exceptions=False) as client:
        answered = client.get("/panics")

    assert (answered.status_code, answered.headers.get("content-type"), answered.json().get("code")) == (500, "application/problem+json", 18), answered.text
