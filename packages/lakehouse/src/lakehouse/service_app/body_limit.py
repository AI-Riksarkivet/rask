"""Request body-size limit — a pure-ASGI middleware that rejects oversized bodies with 413.

The cap is a DoS CEILING, not a business rule: it exists to stop a multi-GB body reaching a buffer, not to say what
a payload should be. A service names the routes whose bodies are bulk (:class:`BulkBodies`) and their ceiling: the
catalog's three Arrow-IPC writes at 256 MiB, the case this was built for. Every other route keeps the small cap, since
a JSON body is read and parsed on the serving loop at whatever size it is admitted ([[LH-606]]).

The Arrow-IPC write endpoints (``/v1/table/{id}/create|insert|merge_insert``) read the whole request body
into memory before the handler runs, so a single unbounded POST — e.g. a multi-GB media blob pushed through
the catalog instead of the direct storage/vending path — OOMs the worker. This enforces the cap *before* the
body is buffered, two ways: a fast ``Content-Length`` reject, and a streaming byte counter that catches a
chunked or length-omitting client. Oversized requests get a problem+json 413 steering them to the direct path.

Pure ASGI (not ``BaseHTTPMiddleware``) so the cap applies as bytes arrive, independent of how the endpoint
reads the body — and so it composes below the app's exception handlers without buffering the request itself.
"""

from __future__ import annotations

from collections.abc import Callable

from lance_namespace import ErrorCode
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problems.envelope import send_problem
from lakehouse.service_app.read_refusal import ReadRefusal


class BulkBodies(BaseModel):
    """The routes of a service whose bodies are bulk, and the ceiling they are held to in place of the app's."""

    model_config = ConfigDict(frozen=True)

    max_bytes: int = Field(ge=1)
    #: Whether the request ``scope`` is for one of those routes.
    matches: Callable[[Scope], bool]


class BodySizeLimitMiddleware:
    """Reject any request whose body exceeds its route's cap with a 413 problem+json response: ``max_bytes``, or
    ``bulk``'s for a route ``bulk`` matches."""

    def __init__(self, app: ASGIApp, *, max_bytes: int, bulk: BulkBodies | None = None) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._bulk = bulk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        cap = self._bulk.max_bytes if self._bulk is not None and self._bulk.matches(scope) else self._max_bytes

        async def reject(send: Send) -> None:
            await self._reject(send, cap)

        # Fast path: a declared Content-Length over the cap is rejected before a single body byte is read.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > cap:
                    await reject(send)
                    return
                break

        # A chunked body, or one longer than its Content-Length, is refused as its bytes arrive, in the read that passes
        # the cap (`lakehouse.service_app.read_refusal`).
        refusal = ReadRefusal(send, reject)
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    await refusal.refuse()
            return message

        await refusal.run(self._app, scope, limited_receive)

    @staticmethod
    async def _reject(send: Send, cap: int) -> None:
        # The spec's envelope, its `code` and `error` included: a generated Lance-Namespace client validates
        # `ErrorResponse` with `code` as a required, no-default field, so a body without it makes the client raise rather
        # than read the refusal.
        await send_problem(
            send,
            ErrorCode.INVALID_INPUT,
            status=413,
            title="Payload Too Large",
            slug="payload-too-large",
            detail=(
                f"request body exceeds the {cap}-byte limit of this route; write large payloads "
                "directly to object storage with vended credentials instead of through the catalog"
            ),
        )


__all__ = ["BodySizeLimitMiddleware", "BulkBodies"]
