"""Refuse NEW work while this process is draining — the admission half of the shutdown flag.

Nine lifespans set `app.state.shutting_down`. Before this module exactly one thing read it: `/readyz`
(:mod:`service_kit.probes`), whose whole effect is to make Kubernetes stop routing new CONNECTIONS.
That is the wrong half for this estate, because the doors that matter are sidecar-delivered: Dapr's
pub/sub delivery does not consult a readiness probe, so a pod that had begun shutting down kept
accepting cascade triggers and run submissions, started work it could not finish, and took the run
down with it. `docs/architecture/batch-processing-invariants.md` B6 names it: "the flag exists, nothing reads it on admission".

§6 of that document rejected the `POST /drain` ENDPOINT — a process-local flag cannot mean "this
deployment is draining" behind a multi-replica Service — and adopted this half. Nothing here decides
that a deployment is draining; it reacts to the flag the lifespan already set for THIS process.

TWO DOORS, TWO ANSWERS, and they are not interchangeable:

* :func:`refuse_when_draining` — an HTTP caller gets **503**. It holds the request and can retry, and
  a `Retry-After` turns that into "come back" rather than a backoff guess. A 4xx would tell the caller
  its request was wrong, which is a lie about a pod that is merely leaving.
* :func:`retry_when_draining` — a sidecar-delivered route gets **RETRY**, never DROP and never
  SUCCESS. DROP is final and these topics carry no DLQ, so dropping a trigger because this replica
  happened to be draining silently cancels a cascade; a SUCCESS ack is worse, being indistinguishable
  from having done the work. RETRY hands the message back to the broker, which redelivers it to a
  replica that is still alive.

Both are pure reads of one boolean. Neither may touch a resource — a drain gate that opened a client
would fail during exactly the window it exists for, which is the rule `/readyz` already states:
once `shutting_down` flips, that is the answer regardless of anything else.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Final

from fastapi import Request
from fastapi.responses import JSONResponse

from service_kit.exceptions import ServiceUnavailableError


if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)


#: Seconds a refused caller is told to wait. A pod's terminationGracePeriod is the natural scale: long
#: enough that the retry lands after this replica is gone, short enough not to stall a real caller.
RETRY_AFTER_SECONDS: Final = 30

_PROBLEM_JSON: Final = "application/problem+json"

#: The subscription verdict. Deliberately the same literal the medallion's own handlers use, so a
#: reader does not have to check whether two spellings mean the same thing.
RETRY: Final[dict[str, str]] = {"status": "RETRY"}


def draining(request: Request) -> bool:
    """Is THIS process shutting down?

    Defaults to False for an app that never set the flag. Three services (ingest, compute, flows) set
    no lifecycle flags at all today, and defaulting an unset flag to "draining" would take them
    permanently out of service the moment this dependency was applied — a gate that fails closed on
    absence would be a worse outage than the one it prevents.
    """
    return bool(getattr(request.app.state, "shutting_down", False))


def refuse_when_draining(request: Request) -> None:
    """FastAPI dependency for an HTTP run door: 503 while draining, transparent otherwise.

    A DOMAIN ERROR, not `HTTPException`. This used to raise the framework's own error with
    `Content-Type: application/problem+json` in `headers` — so the status and `Retry-After` were right
    and the BODY was FastAPI's `{"detail": ...}` wearing a media type that asserts
    `{type,title,status,detail}`. A header that renames a body without changing it is worse than no
    header: it is the first thing a client reads to decide how to parse the payload.

    The old docstring justified that with "raising is not an option here — the caller needs the
    `Retry-After` header, and an exception handler would have to reconstruct it". That was true when it
    was written and is not now: `DomainError` carries `headers` and `register_handlers` passes them
    through, so the header rides the exception and the handler supplies the envelope.

    NOTE FOR THE CALLING APP: `DomainError` subclasses `HTTPException`, so an app that installs only
    `install_problem_handlers` renders this through starlette's built-in handler instead — status and
    header intact, `{"detail": ...}` body again. Every app using this dependency must install
    `register_handlers` too; `tests/test_draining_envelope.py` pins both planes.
    """
    if not draining(request):
        return
    raise ServiceUnavailableError(
        "this instance is shutting down and is not accepting new runs — retry, another replica will serve it",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def retry_when_draining(request: Request) -> dict[str, str] | None:
    """FastAPI dependency for a SIDECAR-delivered route: the RETRY verdict while draining, else None.

    Returns rather than raises, because a subscription route answers 200 with a verdict body — an
    HTTP error at a Dapr sidecar is read as a delivery failure, which happens to retry today and
    would silently become a DROP the moment a resiliency policy treated 5xx as terminal. The handler
    checks the value and returns it unchanged.
    """
    return RETRY if draining(request) else None


def problem_response(detail: str) -> JSONResponse:
    """The 503 as a response object, for a route that composes its own answer rather than depending."""
    return JSONResponse(
        status_code=503,
        media_type=_PROBLEM_JSON,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        content={"type": "about:blank", "title": "draining", "status": 503, "detail": detail},
    )


__all__ = ["RETRY", "RETRY_AFTER_SECONDS", "draining", "problem_response", "refuse_when_draining", "retry_when_draining"]


def arm_drain_on_sigterm(app: FastAPI) -> Callable[[], None]:
    """Flip ``app.state.shutting_down`` the moment SIGTERM arrives, not when the lifespan unwinds.

    WITHOUT THIS THE WHOLE MODULE IS INERT, which is the defect this closes. Every lifespan sets the
    flag in its ``finally`` — i.e. AFTER uvicorn has stopped accepting connections and drained
    in-flight requests. By then a delivery being served has already passed the dependency, and one
    arriving later never reaches the app at all. So the admission guards below refused nothing, ever:
    the module documented a protection it did not provide.

    Kubernetes sends SIGTERM at the START of termination and only then waits out
    ``terminationGracePeriodSeconds``. That window is the whole point — it is exactly when the sidecar
    is still delivering and the pod can still answer. Flipping here turns the grace period into a
    drain instead of a countdown.

    RETURNS a restore callable, and the lifespan must call it. A handler installed per app and never
    removed leaks across a test suite that builds many apps in one process, and — worse — would leave
    a dead app's flag being flipped by a live process's signal.

    Best-effort by construction: ``add_signal_handler`` raises on a loop that does not support it
    (Windows) and ``signal`` raises off the main thread. Neither is a reason to fail a service start,
    so the failure is logged and the process keeps the old behaviour rather than refusing to boot.
    """
    loop = asyncio.get_running_loop()
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except Exception:  # pragma: no cover — non-main thread
        previous = None

    def _flip() -> None:
        # Idempotent, and it must be: a second SIGTERM (an impatient operator, or a runtime that
        # re-sends) must not reset anything or raise out of a signal handler.
        app.state.shutting_down = True
        log.info("drain_armed_by_sigterm")

    try:
        loop.add_signal_handler(signal.SIGTERM, _flip)
    except (NotImplementedError, RuntimeError, ValueError):
        log.warning("could not arm the drain on SIGTERM; the flag flips at lifespan shutdown as before", exc_info=True)
        return lambda: None

    def _restore() -> None:
        with suppress(Exception):
            loop.remove_signal_handler(signal.SIGTERM)
        if previous is not None and callable(previous):
            with suppress(Exception):
                signal.signal(signal.SIGTERM, previous)

    return _restore
