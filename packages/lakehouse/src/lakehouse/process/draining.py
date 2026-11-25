"""The drain flag, flipped the moment SIGTERM arrives.

Every lifespan sets `app.state.shutting_down` as it unwinds, and `/readyz` (:mod:`lakehouse.process.probes`) answers by it. But
uvicorn reaches a lifespan's teardown only after it has stopped accepting and drained the requests in flight, so a
process that waited for it would answer ready through its whole drain. :func:`arm_drain_on_sigterm` flips the flag at
the signal and hands the signal on, so whatever is dispatched after SIGTERM is answered by a process that says it is
stopping.

It is a pure write of one boolean. It may not touch a resource: a drain that opened a client would fail during exactly
the window it exists for, which is the rule `/readyz` already states: once `shutting_down` flips, that is the answer
regardless of anything else.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import TYPE_CHECKING

from lakehouse.process.lifecycle import mark_draining


if TYPE_CHECKING:
    from types import FrameType

    from fastapi import FastAPI

log = logging.getLogger(__name__)


#: What ``signal.getsignal`` can answer once ``None`` (a handler installed outside Python) is excluded.
type _Disposition = Callable[[int, FrameType | None], object] | int | signal.Handlers

#: The app whose drain holds SIGTERM in this process, until its restore runs.
_holder: FastAPI | None = None


def arm_drain_on_sigterm(app: FastAPI) -> Callable[[], None]:
    """Flip ``app.state.shutting_down`` the moment SIGTERM arrives, then hand the signal on.

    The lifespan's ``finally`` sets the same flag, but uvicorn reaches it only after it has stopped
    accepting and drained in-flight requests, so a guard reading the flag there refuses nothing.
    Flipped at the signal, it is already set for whatever uvicorn dispatches after SIGTERM — a probe,
    a request whose admission guard has not yet run — which answers draining (503) instead
    of starting work the process will not finish. A request already past its guard is answered as
    it would have been, including the one whose handler sent a self-SIGTERM: that response is the
    ack for the unit that tripped the maintenance worker's recycle.

    HANDED ON, NEVER SWALLOWED. ``add_signal_handler`` displaces whatever held SIGTERM, and under
    uvicorn that is ``Server.handle_exit``, the only thing that sets ``should_exit``. So ``_flip``
    calls it: uvicorn stops accepting, waits for the requests in flight up to
    ``--timeout-graceful-shutdown`` (the chart's ``rask.shutdownArg``, derived from the pod's grace
    period), cancels the async ones still running and runs the lifespan teardown. A flip that stopped
    at the flag would leave the process up and NotReady until SIGKILL, and turn the maintenance worker's
    self-SIGTERM recycle into a parked lane ([[LH-183]]). Pinned against a real uvicorn by
    ``tests/test_sigterm_still_stops_the_server.py``.

    The default disposition is handed on too — re-raised under ``SIG_DFL``, so the process ends as
    it would have unarmed — and ``SIG_IGN`` stays ignored. A handler not installed from Python
    (``getsignal`` answers ``None``) can be neither called nor restored, so the drain is not armed
    over it.

    ONE ARM PER PROCESS. Armed again before the first is restored, the drain would displace its own
    ``_flip`` and record the loop's no-op trampoline as the handler to hand on to, swallowing SIGTERM.
    A second arm is a wiring bug, so it raises before it touches the loop and the first arm keeps
    handing the signal on.

    RETURNS a restore callable, and the lifespan must call it. A handler installed per app and never
    removed leaks across a test suite that builds many apps in one process, and — worse — would leave
    a dead app's flag being flipped by a live process's signal. The restore also releases the arm, so
    the next lifespan in the process may take it.

    Best-effort by construction: ``add_signal_handler`` raises on a loop that does not support it
    (Windows) and off the main thread. Neither is a reason to fail a service start, so the failure is
    logged and the flag flips at lifespan shutdown instead.

    Raises:
        RuntimeError: The drain is already armed in this process and its restore has not run.
    """
    global _holder
    if _holder is not None:
        # Refused rather than idempotent: an idempotent second arm would leave its own app's flag unflipped.
        raise RuntimeError(f"the SIGTERM drain is already armed in this process, for {_holder.title!r}; one lifespan per process arms it")
    loop = asyncio.get_running_loop()
    held = signal.getsignal(signal.SIGTERM)
    if held is None:
        log.warning("SIGTERM is held by a handler not installed from Python; the drain is not armed over it")
        return lambda: None
    previous: _Disposition = held

    def _flip() -> None:
        # Idempotent, and it must be: a second SIGTERM (an impatient operator, or a runtime that
        # re-sends) must not reset anything, and each one reaches the handler it would have reached.
        mark_draining(app)
        log.info("drain_armed_by_sigterm")
        _hand_on(previous)

    try:
        loop.add_signal_handler(signal.SIGTERM, _flip)
    except NotImplementedError, RuntimeError, ValueError:
        log.warning("could not arm the drain on SIGTERM; the flag flips at lifespan shutdown", exc_info=True)
        return lambda: None
    _holder = app

    def _restore() -> None:
        global _holder
        _holder = None
        # Each step is tried whatever the other did, and a failed one is said: SIGTERM is then still routed to this app's
        # drain, or not back to the handler it displaced, so the next signal may not stop the process as it should.
        try:
            loop.remove_signal_handler(signal.SIGTERM)
        except RuntimeError, ValueError, OSError:
            log.warning("drain_restore_failed", extra={"step": "remove_signal_handler"}, exc_info=True)
        try:
            signal.signal(signal.SIGTERM, previous)
        except RuntimeError, ValueError, OSError:
            log.warning("drain_restore_failed", extra={"step": "signal"}, exc_info=True)

    return _restore


def _hand_on(previous: _Disposition) -> None:
    """Give SIGTERM the effect it had before the drain took it."""
    if callable(previous):
        previous(signal.SIGTERM, None)
    elif previous == signal.SIG_DFL:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.raise_signal(signal.SIGTERM)


__all__ = ["arm_drain_on_sigterm"]
