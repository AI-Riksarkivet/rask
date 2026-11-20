"""A refusal a pure-ASGI layer decides while the app reads the request's body, answered where it is decided.

The body cap and the catalog's write shed each decide inside the ``receive`` they hand the app: the cap once the body
read passes it, the shed when a write first reads with every seat taken. What the app then does with a raise from its
read depends on who reads the body (fastapi 0.140.13, starlette 1.3.1): FastAPI answers any Exception raised while it
reads a route's declared body with its own 400 ("There was an error parsing the body"), and a function middleware
(``BaseHTTPMiddleware``) re-raises an Exception but answers a raised BaseException with a RuntimeError of its own ("No
response returned."). So the refusal is sent from the read itself, everything the app sends after it is dropped, and
the Exception raised to stop the app is swallowed when it comes back, grouped or not. A refusal decided once the app has
begun its answer (a stream reading its body as it answers) has nothing to send, so it is raised on and the server ends
the connection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ReadRefusedError(Exception):
    """Raised into the app from the read a layer refused, to stop it; the layer that refused swallows it."""


class ReadRefusal:
    """One request's refusable read: run the app through :meth:`run`, and call :meth:`refuse` from the read."""

    def __init__(self, send: Send, answer: Callable[[Send], Awaitable[None]]) -> None:
        self._send = send
        self._answer = answer
        self._started = False
        self._refused = False

    async def refuse(self) -> NoReturn:
        """Send the refusal, unless the app has begun its answer, and stop the app's read.

        Raises:
            ReadRefusedError: Always, into the app.
        """
        if not self._started:
            self._refused = True
            await self._answer(self._send)
        raise ReadRefusedError

    async def run(self, app: ASGIApp, scope: Scope, receive: Receive) -> None:
        """Run ``app`` on the request, its body read through ``receive``; what it sends after a refusal is dropped."""
        try:
            await app(scope, receive, self._sent)
        except* ReadRefusedError:
            # Handed back grouped by a function middleware's task group, hence `except*`. Another layer's refusal, or
            # one with no answer sent, goes on up.
            if not self._refused:
                raise

    async def _sent(self, message: Message) -> None:
        if self._refused:
            return
        self._started = self._started or message["type"] == "http.response.start"
        await self._send(message)


__all__ = ["ReadRefusal", "ReadRefusedError"]
