"""A request that has not begun its answer within its deadline is answered 503, and its door runs on to its end.

Without a deadline a request that hangs (a read that never returns, a door waiting on a lock no one releases) holds its
caller for as long as the caller waits. With this one, installed by the app factory, the caller is answered once
``seconds`` pass with no response begun: the spec's 503 (code 17), saying the outcome is unknown.

THE DOOR IS NOT CANCELLED, and that is the decision (owner, 2026-11-12; ADR 0203). A lakehouse door undoes half-done work
in its own ``except`` blocks (a location claim handed back, a parent edge moved back), and a cancellation is no Exception,
so a door cancelled between two steps would skip its undo and leave the step before it standing. Cancelling is safe only
where a handler's work is one transaction that rolls back, and a lakehouse door's is not. So the door runs on in the
background to its commit or its undo, its late answer is dropped, and its end is logged: what it held stays held until it
ends, as it would have without a deadline.

A response already begun when the deadline passes (a long stream) has its caller's answer under way, so it runs on with
no deadline at all.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from lance_namespace import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problem import send_problem
from lakehouse.supervision import is_panic


class RequestDeadlineMiddleware:
    """Answer a request with no response begun within ``seconds`` with a 503, and let its door run on, logged under ``log``."""

    def __init__(self, app: ASGIApp, *, seconds: float, log: logging.Logger) -> None:
        if seconds <= 0:
            raise ValueError(f"a request deadline is a positive number of seconds, got {seconds}")
        self._app = app
        self._seconds = seconds
        self._log = log
        #: The doors still running after their caller was answered, held so none is collected before it ends.
        self._running_on: set[asyncio.Task[None]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        begun = asyncio.Event()
        answered = False

        async def guarded(message: Message) -> None:
            if answered:
                return
            if message["type"] == "http.response.start":
                begun.set()
            await send(message)

        door = asyncio.ensure_future(self._app(scope, receive, guarded))
        try:
            await asyncio.wait_for(asyncio.shield(door), timeout=self._seconds)
            return
        except TimeoutError:
            if begun.is_set():
                await door
                return
        except asyncio.CancelledError:
            # The server cancels the request (it is shutting down): the door is cancelled with it.
            door.cancel()
            with suppress(asyncio.CancelledError):
                await door
            raise
        answered = True
        self._running_on.add(door)
        door.add_done_callback(lambda ended: self._ended(ended, scope))
        self._log.error("request_deadline_passed", extra={"method": scope.get("method"), "path": scope.get("path"), "seconds": self._seconds})
        await _answer(send, self._seconds)

    def _ended(self, door: asyncio.Task[None], scope: Scope) -> None:
        self._running_on.discard(door)
        where = {"method": scope.get("method"), "path": scope.get("path")}
        if door.cancelled():
            self._log.warning("request_past_its_deadline_cancelled", extra=where)
            return
        failure = door.exception()
        if failure is None:
            self._log.info("request_past_its_deadline_finished", extra=where)
        elif isinstance(failure, Exception) or is_panic(failure):
            self._log.error("request_past_its_deadline_failed", exc_info=failure, extra=where)


async def _answer(send: Send, seconds: float) -> None:
    await send_problem(
        send,
        ErrorCode.SERVICE_UNAVAILABLE,
        status=503,
        title="Service Unavailable",
        slug="request-deadline",
        detail=f"the request did not finish within {seconds:.0f} s and runs on; its outcome is unknown, so read what it changes before retrying it",
    )


__all__ = ["RequestDeadlineMiddleware"]
