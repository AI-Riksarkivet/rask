"""Ray Jobs REST submission (the event-driven real-Ray path): submit/re-attach, poll state machine, errors.

Uses httpx.MockTransport (no live Ray) + asyncio.run in sync tests (the repo's async-test convention)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit
from medallion.services.ray_submit import (
    RayJobError,
    _await_success,
    _submission_id,
    _submit_or_reattach,
    submit_stage_job,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(handler))


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def test_submission_id_is_deterministic_and_sanitized() -> None:
    assert _submission_id("bronze", "tok$1") == _submission_id("bronze", "tok$1")  # deterministic
    assert _submission_id("bronze", "tok/1 2") == "ray-bronze-tok-1-2"  # non-id chars → '-'
    assert _submission_id("bronze", None) == "ray-bronze-notoken"


def test_submit_ok_does_not_raise() -> None:
    async def go() -> None:
        async with _client(lambda _r: httpx.Response(200, json={"submission_id": "s"})) as client:
            await _submit_or_reattach(client, "s", {"entrypoint": "x"})

    _run(go())  # no raise == submitted


def test_submit_reattaches_when_already_exists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"detail": "already exists"})
        return httpx.Response(200, json={"status": "RUNNING"})  # the existing job

    async def go() -> None:
        async with _client(handler) as client:
            await _submit_or_reattach(client, "s", {})  # re-attach, no raise

    _run(go())


def test_failed_existing_job_is_deleted_and_resubmitted_fresh() -> None:
    """A redelivery that finds a terminally-FAILED prior job must DELETE it and POST a fresh one (retry on a
    healthy worker) — not re-attach to the dead job forever (the deterministic-id poison)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            # 1st POST: already-exists conflict; 2nd POST (after delete): fresh submit OK.
            return httpx.Response(200, json={"submission_id": "s"}) if "DELETE" in calls else httpx.Response(409)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "FAILED"})  # the prior job terminally failed
        if request.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(404)

    async def go() -> None:
        async with _client(handler) as client:
            await _submit_or_reattach(client, "s", {})

    _run(go())
    assert "DELETE" in calls and calls.count("POST") == 2  # deleted the dead job + resubmitted fresh


def test_submit_raises_when_post_fails_and_no_existing_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom") if request.method == "POST" else httpx.Response(404)

    async def go() -> None:
        async with _client(handler) as client:
            await _submit_or_reattach(client, "s", {})

    with pytest.raises(RayJobError, match="submit"):
        _run(go())


def test_await_success_returns_on_succeeded() -> None:
    async def go() -> None:
        async with _client(lambda _r: httpx.Response(200, json={"status": "SUCCEEDED"})) as client:
            await _await_success(client, "sub", 0.001)

    _run(go())


@pytest.mark.parametrize("bad", ["FAILED", "STOPPED"])
def test_await_success_raises_on_terminal_bad(bad: str) -> None:
    async def go() -> None:
        async with _client(lambda _r: httpx.Response(200, json={"status": bad, "message": "x"})) as client:
            await _await_success(client, "sub", 0.001)

    with pytest.raises(RayJobError, match=bad):
        _run(go())


def test_await_success_polls_through_running() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "SUCCEEDED" if calls["n"] >= 3 else "RUNNING"})

    async def go() -> None:
        async with _client(handler) as client:
            await _await_success(client, "sub", 0.001)

    _run(go())
    assert calls["n"] == 3


def test_await_success_tolerates_transient_poll_blips() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:  # two transient 5xx, under the tolerance, then success
            return httpx.Response(503, text="blip")
        return httpx.Response(200, json={"status": "SUCCEEDED"})

    async def go() -> None:
        async with _client(handler) as client:
            await _await_success(client, "sub", 0.001)

    _run(go())
    assert calls["n"] == 3


def test_await_success_gives_up_after_too_many_poll_errors() -> None:
    async def go() -> None:
        async with _client(lambda _r: httpx.Response(503, text="down")) as client:
            await _await_success(client, "sub", 0.001)

    with pytest.raises(RayJobError, match="poll"):
        _run(go())


def _stub_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr(ray_submit.httpx, "AsyncClient", factory)


def _settings(**overrides: Any) -> MedallionSettings:
    base = {
        "ray_enabled": True,
        "compute_enabled": True,  # ray requires compute (fail-fast validator)
        "ray_poll_interval_seconds": 0.001,
        "ray_job_timeout_seconds": 0.05,
    }
    return MedallionSettings.model_validate(base | overrides)


def test_submit_stage_job_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"submission_id": "s1"})
        return httpx.Response(200, json={"status": "SUCCEEDED"})

    _stub_client(monkeypatch, handler)
    _run(submit_stage_job(_settings(), from_uri="a", to_uri="b", stage="bronze", token="t"))  # no raise


def test_submit_stage_job_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"submission_id": "s1"})
        return httpx.Response(200, json={"status": "RUNNING"})  # never finishes

    _stub_client(monkeypatch, handler)
    with pytest.raises(RayJobError, match="did not finish"):
        _run(submit_stage_job(_settings(), from_uri="a", to_uri="b", stage="bronze", token="t"))
