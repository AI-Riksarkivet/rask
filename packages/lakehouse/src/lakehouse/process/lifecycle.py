"""The lifecycle contract ``/readyz`` reads — one typed home for two booleans.

``lakehouse.process.probes`` answers readiness off ``app.state.startup_complete`` and
``app.state.shutting_down``. This module is where the two names are spelled and read, so a probe and the
lifespan that sets them share one definition rather than a naming agreement: a lifespan that logs the
string ``"startup_complete"`` without setting the attribute leaves ``/readyz`` answering ``starting`` for the
whole life of the pod, and the drain never begins.

Absence reads as "not started, not draining". That asymmetry is deliberate and matches
:mod:`lakehouse.process.draining`: defaulting an unset drain flag to True would take an app that never
set it permanently out of service, while defaulting ``started`` to True would let a half-built
app claim readiness.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.datastructures import State


if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

#: The two ``app.state`` attribute names. Named once so a rename cannot half-land.
_STARTED_FLAG = "startup_complete"
_DRAINING_FLAG = "shutting_down"


def mark_starting(app: FastAPI) -> None:
    """Declare this process starting: not started and not draining, whatever an earlier lifespan of the same app left
    (a test client enters one app's lifespan many times in one process)."""
    setattr(app.state, _STARTED_FLAG, False)
    setattr(app.state, _DRAINING_FLAG, False)


def mark_started(app: FastAPI) -> None:
    """Declare this process's startup finished — called once the base lifespan has entered."""
    setattr(app.state, _STARTED_FLAG, True)
    setattr(app.state, _DRAINING_FLAG, False)


def mark_draining(app: FastAPI) -> None:
    """Declare this process draining: it must stop admitting new work and report unready.

    Clears ``started`` as well, because the two are read independently and a process that is both
    "started" and "draining" is only ever the second thing.
    """
    setattr(app.state, _DRAINING_FLAG, True)
    setattr(app.state, _STARTED_FLAG, False)


def is_started(app_or_state: FastAPI | State) -> bool:
    """Has startup completed? False for an app that never declared it."""
    return bool(getattr(_state(app_or_state), _STARTED_FLAG, False))


def is_draining(app_or_state: FastAPI | State) -> bool:
    """Is this process draining? False for an app that never declared it."""
    return bool(getattr(_state(app_or_state), _DRAINING_FLAG, False))


def _state(app_or_state: FastAPI | State) -> State:
    """Accept either the app or its ``state``.

    A probe handler holds ``request.app.state``; a lifespan holds the app. Both must be able to ask
    the same question.
    """
    return app_or_state if isinstance(app_or_state, State) else app_or_state.state


async def closed_quietly(close: Callable[[], Awaitable[object]]) -> None:
    """Close a client as a lifespan ends; a close its server no longer answers is the end it was closed for, so a failure
    of it is passed over rather than raised over the other closes the lifespan still has to make. It is logged at DEBUG
    with its traceback: it changes nothing an operator acts on, and a bus outage releases a failed client every attempt."""
    try:
        await close()
    except Exception:
        log.debug("client_close_failed", extra={"close": getattr(close, "__qualname__", type(close).__name__)}, exc_info=True)


__all__ = ["closed_quietly", "is_draining", "is_started", "mark_draining", "mark_started", "mark_starting"]
