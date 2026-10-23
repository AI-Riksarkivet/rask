"""How the catalog reads a request's path: a bare `+` is a space, and an escaped `/` names nothing ([[LH-526]]).

pylance's Rust `RestNamespace` sends a space in an identifier as a bare `+` and a `+` as `%2B` (measured, ADR 0173),
while the server decodes only percent-escapes into the path it routes on, so a name holding a space would reach its door
spelled with a `+`. The catalog reads its path from the bytes the client sent (`raw_path`), decoded as uvicorn decodes
them but with a bare `+` read as the space it stands for. A `%2B` stays a `+`, which no name holds (ADR 0173 §4), so a
naming door refuses it rather than read a name the client never sent. A client escaping a space as `%20`, as the spec's
generated urllib3 client does, reads the same either way.

A `/` escaped as `%2F` is decoded before routing and splits the segment holding it, so no route can carry one, and an
identifier holding one would answer as a route that does not exist. No name holds a `/`, so a path spelling one is
refused here, as the spec's malformed request.

Pure ASGI and outermost, so every layer beneath it reads the one path: the write shed, the body cap, the gates that
classify a write by its path, routing and the doors.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import unquote

from lance_namespace import InvalidInputError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from service_kit.lakehouse.ns_errors import problem_detail
from service_kit.problem import PROBLEM_JSON


#: `/` escaped, in either case.
_ESCAPED_SLASH: Final = re.compile(rb"%2f", re.IGNORECASE)


class PathReadingMiddleware:
    """Read a bare ``+`` in a request's path as a space, and refuse a path holding an escaped ``/`` with the spec's 400."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw = scope.get("raw_path") if scope["type"] == "http" else None
        if raw is None:
            await self._app(scope, receive, send)
            return
        if _ESCAPED_SLASH.search(raw):
            await _escaped_slash_refusal()(scope, receive, send)
            return
        if b"+" in raw:
            scope = {**scope, "path": unquote(raw.decode("ascii").replace("+", " "))}
        await self._app(scope, receive, send)


def _escaped_slash_refusal() -> JSONResponse:
    """The spec's InvalidInput for a path holding ``%2F``, rendered here: this layer answers before the app's handlers."""
    status, body = problem_detail(InvalidInputError("a path segment holds '/', escaped as %2F: no name holds '/' (ADR 0173 §4), and no route carries one"))
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)
