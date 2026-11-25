"""The drain flag flips at SIGTERM, and SIGTERM still reaches the handler the drain displaced.

`/readyz` (`lakehouse.process.probes`) answers by `app.state.shutting_down`, and a lifespan that
sets it only in its `finally` sets it after uvicorn has stopped accepting connections and drained
in-flight requests — so the process would answer ready through its whole drain. `arm_drain_on_sigterm` flips it when the
signal arrives instead. Owner ruling 2026-08-25.

These run in-process, so every test that raises SIGTERM first installs a SENTINEL handler for the
drain to displace: it stands where uvicorn's `handle_exit` stands, and it is how each test sees the
signal handed on without the default disposition terminating pytest. The real uvicorn is exercised in
`test_sigterm_still_stops_the_server.py`.
"""

import asyncio
import logging
import signal
from collections.abc import Iterator
from types import FrameType
from typing import Any

import pytest
from fastapi import FastAPI

from lakehouse.process import draining
from lakehouse.process.draining import arm_drain_on_sigterm


async def _settle() -> None:
    """Let the loop actually run the signal callback.

    `sleep(0)` is not enough: the handler is scheduled from the signal wakeup fd, so it needs a real
    loop iteration rather than a single yield.
    """
    await asyncio.sleep(0.05)


@pytest.fixture
def handed() -> Iterator[list[int]]:
    """A Python-level SIGTERM handler recording each signal it is handed, restored afterwards."""
    received: list[int] = []

    def _record(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)

    original = signal.signal(signal.SIGTERM, _record)
    try:
        yield received
    finally:
        signal.signal(signal.SIGTERM, original)


def _app() -> FastAPI:
    app = FastAPI()
    app.state.shutting_down = False
    return app


@pytest.mark.asyncio
async def test_a_second_signal_is_harmless_and_handed_on_too(handed: list[int]) -> None:
    """An impatient operator sends two. A signal handler that raised would be unhandleable, and the
    displaced handler decides what a second one means (uvicorn's escalates only on SIGINT)."""
    app = _app()
    disarm = arm_drain_on_sigterm(app)
    try:
        signal.raise_signal(signal.SIGTERM)
        await _settle()
        signal.raise_signal(signal.SIGTERM)
        await _settle()

        assert app.state.shutting_down is True
        assert handed == [signal.SIGTERM, signal.SIGTERM], handed
    finally:
        disarm()


@pytest.mark.asyncio
async def test_a_second_arm_is_refused_while_the_first_holds_sigterm(handed: list[int]) -> None:
    """A process has one SIGTERM disposition, so a second app cannot arm over the first. The refusal comes before
    the loop is touched, so the first still hands the signal on exactly once, and once it is disarmed the next
    app may arm."""
    first, second = _app(), _app()
    disarm = arm_drain_on_sigterm(first)
    try:
        with pytest.raises(RuntimeError, match="already armed"):
            arm_drain_on_sigterm(second)
        signal.raise_signal(signal.SIGTERM)
        await _settle()

        assert handed == [signal.SIGTERM], f"the first arm no longer hands SIGTERM on once: {handed}"
        assert (first.state.shutting_down, second.state.shutting_down) == (True, False)
    finally:
        disarm()

    arm_drain_on_sigterm(second)()


@pytest.mark.asyncio
async def test_an_ignored_sigterm_stays_ignored_and_is_restored_as_ignored() -> None:
    """`SIG_IGN` is not callable, so it is the case a `callable()` check drops on the way back."""
    original = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        app = _app()
        disarm = arm_drain_on_sigterm(app)
        signal.raise_signal(signal.SIGTERM)
        await _settle()
        disarm()

        assert app.state.shutting_down is True
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN, "disarming turned an ignored SIGTERM into a fatal one"
    finally:
        signal.signal(signal.SIGTERM, original)


@pytest.mark.asyncio
async def test_a_handler_not_installed_from_python_is_not_armed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """`getsignal` answers None for a handler installed outside Python. It can be neither called nor
    restored, so displacing it would take SIGTERM's meaning away for good."""
    installed: list[Any] = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(draining.signal, "getsignal", lambda _sig: None)
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **_k: installed.append(a))

    arm_drain_on_sigterm(_app())()

    assert installed == [], "the drain displaced a handler it can neither hand on to nor restore"


@pytest.mark.asyncio
async def test_disarming_stops_a_dead_app_from_being_flipped(handed: list[int]) -> None:
    """Why the helper returns a restore callable and the lifespan must call it: a handler installed
    per app and never removed leaks across a suite that builds many apps in one process -- and would
    leave a DEAD app's flag being flipped by a live process's signal.

    SIGTERM is raised on the loop exactly as the restore left it. A loop handler installed first would
    replace the dead app's `_flip` in the loop's table and hide a restore that never removed it; left
    in place, that `_flip` still runs off the wakeup fd and hands the signal on a second time."""
    dead = _app()
    arm_drain_on_sigterm(dead)()
    assert callable(signal.getsignal(signal.SIGTERM)), "the restore did not put the recording handler back, and raising SIGTERM now would end pytest"

    signal.raise_signal(signal.SIGTERM)
    await _settle()

    assert dead.state.shutting_down is False, "a disarmed app was still flipped"
    assert handed == [signal.SIGTERM], f"the restored handler must receive SIGTERM exactly once: {handed}"


@pytest.mark.asyncio
async def test_a_restore_step_that_fails_is_said_and_the_other_is_still_taken(
    handed: list[int], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A restore step that fails leaves SIGTERM routed where the process no longer means it to go, so it is logged at
    WARNING with its traceback, and the other step is taken all the same."""
    del handed
    loop = asyncio.get_running_loop()
    remove = loop.remove_signal_handler
    restore = arm_drain_on_sigterm(_app())

    def _refuse(_signum: int) -> bool:
        raise RuntimeError("the loop refused to remove the handler")

    monkeypatch.setattr(loop, "remove_signal_handler", _refuse)
    with caplog.at_level(logging.WARNING, logger=draining.__name__):
        restore()
    monkeypatch.undo()
    put_back = signal.getsignal(signal.SIGTERM)
    remove(signal.SIGTERM)

    told = [(r.levelno, vars(r)["step"], r.exc_info is not None) for r in caplog.records if r.getMessage() == "drain_restore_failed"]
    assert told == [(logging.WARNING, "remove_signal_handler", True)]
    assert callable(put_back) and getattr(put_back, "__name__", "") == "_record", "the displaced handler was not put back"


@pytest.mark.asyncio
async def test_a_loop_that_cannot_take_a_handler_does_not_fail_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort by construction. `add_signal_handler` raises on Windows and off the main thread,
    and neither is a reason to refuse to boot a service -- the flag then flips at lifespan shutdown."""
    app = _app()
    loop = asyncio.get_running_loop()

    def _refuse(*_a: Any, **_k: Any) -> None:
        raise NotImplementedError("this loop has no signal handlers")

    monkeypatch.setattr(loop, "add_signal_handler", _refuse)

    disarm = arm_drain_on_sigterm(app)  # must not raise

    disarm()  # and the returned callable must be safe to call
