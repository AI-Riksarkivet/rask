"""A request that has not begun its answer within its deadline is answered 503, or 500 once its write has begun a commit,
and its door runs on to its end.

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

Each request's writes report their commit to its mark (`lakehouse.process.request_commits`; owner, 2026-11-28). One whose
write has begun its commit when the deadline passes is answered 500 with code 18 under
:class:`~lakehouse.problems.ns_errors.CommitOutcomeUnknownError`'s name, never the retryable 503: the commit may have
applied, or may apply as the door runs on (ADR 0230 §6). One answered 503 is closed to every commit after it, so a write
its door reaches later is refused and rolls back, and the 503 stays true for the catalog's database. What a door changes
elsewhere reports to no mark and is not covered: a Lance commit, which goes to the object store, and an OpenFGA write; for
those the 503 still asks its caller to read what the request changed before retrying it.
"""

import asyncio
import logging
from contextlib import suppress

from lance_namespace import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problems.envelope import send_problem
from lakehouse.problems.ns_errors import CommitOutcomeUnknownError, error_response
from lakehouse.process.request_commits import CommitMark, LateCommitRefusedError, marking
from lakehouse.process.supervision import is_fault


class RequestDeadlineMiddleware:
    """Answer a request with no response begun within ``seconds`` with a 503, or a 500 once its write has begun a commit
    (`lakehouse.process.request_commits`), and let its door run on, logged under ``log``."""

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
        mark = CommitMark()
        answered = False

        async def guarded(message: Message) -> None:
            if answered:
                return
            if message["type"] == "http.response.start":
                begun.set()
            await send(message)

        async def serve() -> None:
            marking(mark)
            await self._app(scope, receive, guarded)

        door = asyncio.create_task(serve())
        try:
            await asyncio.wait_for(asyncio.shield(door), timeout=self._seconds)
            return
        except TimeoutError:
            if begun.is_set():
                await door
                return
        except asyncio.CancelledError:
            await self._cancel(door, scope, mark)
            raise
        answered = True
        self._running_on.add(door)
        door.add_done_callback(lambda ended: self._ended(ended, scope))
        await self._answer_late(scope, receive, send, mark=mark)

    async def _cancel(self, door: asyncio.Task[None], scope: Scope, mark: CommitMark) -> None:
        """The server cancels the request (it is shutting down): its door is cancelled with it, and a commit it had begun,
        whose outcome nobody is told, is said."""
        if mark.begun:
            self._log.warning("request_cancelled_after_its_commit_began", extra={"method": scope.get("method"), "path": scope.get("path")})
        door.cancel()
        with suppress(asyncio.CancelledError):
            await door

    async def _answer_late(self, scope: Scope, receive: Receive, send: Send, *, mark: CommitMark) -> None:
        """Answer a request past its deadline: as one that may have applied once its write has begun a commit, else 503."""
        commit_begun = mark.settle()
        self._log.error(
            "request_deadline_passed",
            extra={"method": scope.get("method"), "path": scope.get("path"), "seconds": self._seconds, "commit_begun": commit_begun},
        )
        if commit_begun:
            await _answer_outcome_unknown(scope, receive, send, seconds=self._seconds)
        else:
            await _answer_unavailable(send, self._seconds)

    def _ended(self, door: asyncio.Task[None], scope: Scope) -> None:
        self._running_on.discard(door)
        where = {"method": scope.get("method"), "path": scope.get("path")}
        if door.cancelled():
            self._log.warning("request_past_its_deadline_cancelled", extra=where)
            return
        failure = door.exception()
        if failure is None:
            self._log.info("request_past_its_deadline_finished", extra=where)
        elif isinstance(failure, LateCommitRefusedError):
            self._log.warning("request_past_its_deadline_refused_its_commit", extra=where)
        elif is_fault(failure):
            self._log.error("request_past_its_deadline_failed", exc_info=failure, extra=where)


async def _answer_unavailable(send: Send, seconds: float) -> None:
    await send_problem(
        send,
        ErrorCode.SERVICE_UNAVAILABLE,
        status=503,
        title="Service Unavailable",
        slug="request-deadline",
        detail=f"the request did not finish within {seconds:.0f} s and runs on; its outcome is unknown, so read what it changes before retrying it",
    )


async def _answer_outcome_unknown(scope: Scope, receive: Receive, send: Send, *, seconds: float) -> None:
    # The handlers' own answer to the error, so its body is the one the catalog's database answers it with, its 5xx detail
    # held back; the deadline's own record says why.
    unknown = CommitOutcomeUnknownError(f"the request did not finish within {seconds:.0f} s, and its commit had begun, so it may have applied")
    await error_response(unknown)(scope, receive, send)


__all__ = ["RequestDeadlineMiddleware"]
