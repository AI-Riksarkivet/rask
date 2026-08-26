"""The drain flag was flipped too late for any admission guard to matter.

`retry_when_draining` / `refuse_when_draining` read `app.state.shutting_down`, and every lifespan set
it in its `finally` -- which uvicorn only reaches AFTER it has stopped accepting connections and
drained in-flight requests. By then a delivery being served has already passed the dependency, and
one arriving later never reaches the app at all. The guards refused nothing, ever: the module
documented a protection it did not provide.

Kubernetes sends SIGTERM at the START of termination and only then waits out
`terminationGracePeriodSeconds`. Flipping there turns the grace period into a drain instead of a
countdown. Owner ruling 2026-08-25.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest
from fastapi import FastAPI

from service_kit.draining import arm_drain_on_sigterm


async def _settle() -> None:
    """Let the loop actually run the signal callback.

    `sleep(0)` is not enough: the handler is scheduled from the signal wakeup fd, so it needs a real
    loop iteration rather than a single yield.
    """
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_SIGTERM_flips_the_flag_immediately() -> None:
    """THE WEDGE. Before this, nothing set the flag until the lifespan unwound."""
    app = FastAPI()
    app.state.shutting_down = False
    disarm = arm_drain_on_sigterm(app)
    try:
        signal.raise_signal(signal.SIGTERM)
        await _settle()

        assert app.state.shutting_down is True, "the grace period is still a countdown, not a drain"
    finally:
        disarm()


@pytest.mark.asyncio
async def test_a_SECOND_signal_is_harmless() -> None:
    """An impatient operator sends two. A signal handler that raised would be unhandleable."""
    app = FastAPI()
    app.state.shutting_down = False
    disarm = arm_drain_on_sigterm(app)
    try:
        signal.raise_signal(signal.SIGTERM)
        await _settle()
        signal.raise_signal(signal.SIGTERM)
        await _settle()

        assert app.state.shutting_down is True
    finally:
        disarm()


@pytest.mark.asyncio
async def test_DISARMING_stops_a_dead_app_from_being_flipped() -> None:
    """Why the helper returns a restore callable and the lifespan must call it: a handler installed
    per app and never removed leaks across a suite that builds many apps in one process -- and would
    leave a DEAD app's flag being flipped by a live process's signal."""
    dead = FastAPI()
    dead.state.shutting_down = False
    disarm = arm_drain_on_sigterm(dead)
    disarm()

    # A SENTINEL, because `disarm` restores SIGTERM's DEFAULT action and raising it here would kill
    # pytest outright — which is itself worth knowing: the restore is only safe at real shutdown, and
    # this test would be the thing that discovered otherwise.
    fired: list[bool] = []
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: fired.append(True))
    try:
        signal.raise_signal(signal.SIGTERM)
        await _settle()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    assert fired == [True], "the sentinel never ran, so this proves nothing about the app's handler"
    assert dead.state.shutting_down is False, "a disarmed app was still flipped"


@pytest.mark.asyncio
async def test_a_loop_that_cannot_take_a_handler_does_not_fail_the_START(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort by construction. `add_signal_handler` raises on Windows and off the main thread,
    and neither is a reason to refuse to boot a service -- the process keeps the old behaviour."""
    app = FastAPI()
    app.state.shutting_down = False
    loop = asyncio.get_running_loop()

    def _refuse(*_a: Any, **_k: Any) -> None:
        raise NotImplementedError("this loop has no signal handlers")

    monkeypatch.setattr(loop, "add_signal_handler", _refuse)

    disarm = arm_drain_on_sigterm(app)  # must not raise

    disarm()  # and the returned callable must be safe to call
