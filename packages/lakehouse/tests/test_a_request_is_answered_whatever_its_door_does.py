"""A lakehouse app answers every request with the spec's problem body, whatever its door does (ADR 0203).

`build_service_app` installs two layers inside the request's trace for catalog, lineage and maintenance. A door
that has begun no answer within the request deadline is answered 503 (code 17) and runs on to its own end, never
cancelled, since a lakehouse door undoes its half-done work in its own ``except`` blocks. A door in which a native library
panics, which pyo3 raises as a BaseException that no Exception handler catches, is answered 500 (code 18) as any other
failure is, rather than by the server's bare text 500.

Every answer, a door's or a layer's refusal in front of it, tells any cache between the caller and the service to keep
none of it ([[LH-486]]): a door may answer the credentials it vends, and what one caller is answered is no other's.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from lakehouse.process.lifecycle import mark_started
from lakehouse.schemas.health import Readiness, ReadinessStatus
from lakehouse.service_app.factory import build_service_app


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

    return build_service_app(
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


def _a_doors_answer(client: TestClient) -> httpx.Response:
    return client.get("/answers")


def _a_refusal_in_front_of_the_door(client: TestClient) -> httpx.Response:
    """A body over the app's cap, which the body cap refuses before the door runs."""
    return client.post("/answers", content=b"x" * 2048)


def _an_answer_naming_its_own_caching(client: TestClient) -> httpx.Response:
    return client.get("/cached")


def _a_door_that_fails_unexpectedly(client: TestClient) -> httpx.Response:
    """A door raising an exception no handler names, which the server's own error layer answers."""
    return client.get("/fails")


@pytest.mark.parametrize(
    ("ask", "status", "cache_control", "vary"),
    [
        pytest.param(_a_doors_answer, 200, "no-store", "Authorization", id="a-doors-answer"),
        pytest.param(_a_refusal_in_front_of_the_door, 413, "no-store", "Authorization", id="a-refusal-in-front-of-the-door"),
        pytest.param(_an_answer_naming_its_own_caching, 200, "private, max-age=60", "Accept, Authorization", id="an-answer-naming-its-own-caching"),
        pytest.param(_a_door_that_fails_unexpectedly, 500, "no-store", "Authorization", id="a-door-that-fails-unexpectedly"),
    ],
)
def test_every_answer_tells_a_cache_to_keep_none_of_it_unless_its_door_says_otherwise(
    ask: Callable[[TestClient], httpx.Response], status: int, cache_control: str, vary: str
) -> None:
    app = _app(deadline_seconds=30.0)

    @app.get("/answers")
    @app.post("/answers")
    async def _answers() -> dict[str, str]:
        return {"storage_options": "a credential"}

    @app.get("/cached")
    async def _cached(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "private, max-age=60"
        response.headers["Vary"] = "Accept"
        return {"answer": "public knowledge"}

    @app.get("/fails")
    async def _fails() -> None:
        raise RuntimeError("nothing names this failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        answered = ask(client)

    assert (answered.status_code, answered.headers.get("cache-control"), answered.headers.get("vary")) == (status, cache_control, vary), answered.text
