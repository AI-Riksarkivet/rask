"""The lifecycle contract ``/readyz`` reads — one typed home for two booleans.

``service_kit.probes`` answers readiness off ``app.state.startup_complete`` and
``app.state.shutting_down``, and until this module those two names were pure convention:
untyped ``getattr(state, ..., False)`` on one side, a hand-rolled assignment in every service's
own lifespan on the other. Eleven copies of the convention meant eleven chances to miss it, and
five of them did — every app built by :func:`service_kit.make_service_app` (compute,
controlplane, ingest, flows, notifications) set neither flag, so ``/readyz`` reported
``starting`` for the whole life of the pod and the drain never began. ``compute``'s lifespan
logs the string ``"startup_complete"`` without ever setting the attribute, which is exactly how
a convention fails: it looks done.

So the FACTORY now owns the invariant (see ``make_service_app``'s lifespan wrapper) and this
module is the only place the two names are spelled. A service that builds its own ``FastAPI``
keeps setting them itself — through these functions rather than by assignment, so the contract
has one definition rather than a naming agreement.

Absence reads as "not started, not draining". That asymmetry is deliberate and matches
:mod:`service_kit.draining`: defaulting an unset drain flag to True would take an app that never
set it permanently out of service, while defaulting ``started`` to True would let a half-built
app claim readiness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import State


if TYPE_CHECKING:
    from fastapi import FastAPI


#: The two ``app.state`` attribute names. Named once so a rename cannot half-land.
STARTED_FLAG = "startup_complete"
DRAINING_FLAG = "shutting_down"


def mark_started(app: FastAPI) -> None:
    """Declare this process's startup finished — called once the base lifespan has entered."""
    setattr(app.state, STARTED_FLAG, True)
    setattr(app.state, DRAINING_FLAG, False)


def mark_draining(app: FastAPI) -> None:
    """Declare this process draining: it must stop admitting new work and report unready.

    Clears ``started`` as well, because the two are read independently and a process that is both
    "started" and "draining" is only ever the second thing.
    """
    setattr(app.state, DRAINING_FLAG, True)
    setattr(app.state, STARTED_FLAG, False)


def is_started(app_or_state: FastAPI | State) -> bool:
    """Has startup completed? False for an app that never declared it."""
    return bool(getattr(_state(app_or_state), STARTED_FLAG, False))


def is_draining(app_or_state: FastAPI | State) -> bool:
    """Is this process draining? False for an app that never declared it."""
    return bool(getattr(_state(app_or_state), DRAINING_FLAG, False))


def _state(app_or_state: FastAPI | State) -> State:
    """Accept either the app or its ``state``.

    A probe handler holds ``request.app.state``; a lifespan holds the app. Both must be able to ask
    the same question.
    """
    return app_or_state if isinstance(app_or_state, State) else app_or_state.state


__all__ = ["DRAINING_FLAG", "STARTED_FLAG", "is_draining", "is_started", "mark_draining", "mark_started"]
