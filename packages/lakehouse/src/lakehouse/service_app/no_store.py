"""Every answer of a lakehouse app tells a cache to keep none of it, unless its door names its own caching ([[LH-486]]).

A door may answer credentials: DescribeTable and DeclareTable vend the store session a stock client signs with, in the
answer's ``storage_options`` (``lance_docs/ns_catalog/spec.yaml``, DescribeTableResponse), and the management vend doors
answer the same. HTTP lets any cache between the caller and the service store an answer that carries no
``Cache-Control``, and serve it to the next caller. So the app factory puts this layer around the app's own entry point
(`lakehouse.service_app.factory`), where every refusal passes too, the server's own answer to an unexpected exception included:
an answer with no ``Cache-Control`` of its own gets ``no-store``, and every answer varies on ``Authorization``, since
what one caller is answered is no other's.

``no-store`` for every answer, never only where credentials are: a door added later then needs nothing to be safe, and
no door here answers a cache's conditional request, so a cache has nothing to offer (the blob door's ``If-Range``
resumes a download and is decided per request, with no stored answer). A door whose answer may be kept says so itself,
and that header stands.

A pure ASGI layer, where a ``BaseHTTPMiddleware`` is the usual choice for a response header: it wraps the app's entry
point, which takes an ASGI app, because a middleware the app installs sits inside Starlette's own error layer and never
sees the 500 that layer answers.
"""

from __future__ import annotations

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_NO_STORE: Final = "no-store"
_VARIES_ON: Final = "Authorization"


class NoStoreMiddleware:
    """Set ``Cache-Control: no-store`` on an answer that names no caching of its own, and vary every answer on ``Authorization``."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def uncached(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", _NO_STORE)
                headers.add_vary_header(_VARIES_ON)
            await send(message)

        await self._app(scope, receive, uncached)


__all__ = ["NoStoreMiddleware"]
