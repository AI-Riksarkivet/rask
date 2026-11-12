"""A native library's panic inside a request is answered 500 with the spec's problem body, and the process serves on.

pylance and pyarrow are native code, and pyo3 raises a Rust panic as a BaseException that is no Exception
(:func:`lakehouse.supervision.is_panic`). Every handler the app installs catches Exceptions, so without this layer a panic
passes them all and reaches the server, which answers a bare text 500 and logs it under its own logger, with nothing
that names the service or carries the request's id. Installed by the app factory inside the request-id layer, this
answers it as the problem handlers answer any other failure, logs it with its traceback under the service's logger, and
lets the process serve the next request: one table that makes Lance panic takes down that request, not the replica
(ADR 0203). A request to stop (a cancellation, ``SystemExit``) passes through untouched.
"""

from __future__ import annotations

import logging

from lance_namespace import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problem import send_problem
from lakehouse.supervision import is_panic


class PanicCatcherMiddleware:
    """Answer a panic that escapes the app with a 500 problem body, logged under ``log``."""

    def __init__(self, app: ASGIApp, *, log: logging.Logger) -> None:
        self._app = app
        self._log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self._app(scope, receive, tracked)
        except BaseException as exc:
            if not is_panic(exc):
                raise
            self._log.error("request_panicked", exc_info=exc, extra={"method": scope.get("method"), "path": scope.get("path")})
            if started:
                # Half a response is on the wire: the server closes the connection, which is all a caller can be told now.
                raise
            await _answer(send)


async def _answer(send: Send) -> None:
    await send_problem(
        send,
        ErrorCode.INTERNAL,
        status=500,
        title="Internal Server Error",
        slug="internal-error",
        detail="the request failed inside a native library; nothing it changed is known, so read what it changes before retrying it",
    )


__all__ = ["PanicCatcherMiddleware"]
