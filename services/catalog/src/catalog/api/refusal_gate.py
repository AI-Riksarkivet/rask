"""A request layer that answers a request its decision refuses, before the door runs, and passes every other untouched.

Pure ASGI rather than an ``http`` middleware function, which Starlette runs as a ``BaseHTTPMiddleware`` that wraps every
answer, Arrow streams included, in an anyio memory stream and a task group: a layer every request passes, in a service
that streams large bodies, is pure ASGI (fastapi production-patterns.md § Middleware). The decision reads only the
request's head (its method, its routed path and what the lifespan wired), so a refused request's body is never read and
a passed request reaches the door exactly as it arrived.
"""

from __future__ import annotations

from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send


#: A decision from a request's head alone: the answer to send in place of its door, or ``None`` to let it through.
type Refuser = Callable[[Request], Response | None]


class RefusalGate:
    """Answer a request ``refuser`` refuses with the refusal it returns; pass every other request, and every scope that is
    not HTTP, to ``app`` untouched."""

    def __init__(self, app: ASGIApp, *, refuser: Refuser) -> None:
        self._app = app
        self._refuser = refuser

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        answer = self._refuser(Request(scope)) if scope["type"] == "http" else None
        if answer is None:
            await self._app(scope, receive, send)
            return
        await answer(scope, receive, send)
