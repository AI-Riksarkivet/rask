"""Request body-size limit — a pure-ASGI middleware that rejects oversized bodies with 413.

The cap is a DoS CEILING, not a business rule: it exists to stop a multi-GB body reaching a buffer, not to say what
a payload should be. The catalog sets 256 MiB, because Arrow-IPC writes are exactly the case this was built for.

The Arrow-IPC write endpoints (``/v1/table/{id}/create|insert|merge_insert``) read the whole request body
into memory before the handler runs, so a single unbounded POST — e.g. a multi-GB media blob pushed through
the catalog instead of the direct storage/vending path — OOMs the worker. This enforces the cap *before* the
body is buffered, two ways: a fast ``Content-Length`` reject, and a streaming byte counter that catches a
chunked or length-omitting client. Oversized requests get a problem+json 413 steering them to the direct path.

Pure ASGI (not ``BaseHTTPMiddleware``) so the cap applies as bytes arrive, independent of how the endpoint
reads the body — and so it composes below the app's exception handlers without buffering the request itself.
"""

from __future__ import annotations

from lance_namespace import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problem import send_problem
from lakehouse.read_refusal import ReadRefusal


class BodySizeLimitMiddleware:
    """Reject any request whose body exceeds ``max_bytes`` with a 413 problem+json response."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Fast path: a declared Content-Length over the cap is rejected before a single body byte is read.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self._max_bytes:
                    await self._reject(send)
                    return
                break

        # A chunked body, or one longer than its Content-Length, is refused as its bytes arrive, in the read that passes
        # the cap (`lakehouse.read_refusal`).
        refusal = ReadRefusal(send, self._reject)
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    await refusal.refuse()
            return message

        await refusal.run(self._app, scope, limited_receive)

    async def _reject(self, send: Send) -> None:
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
                f"request body exceeds the {self._max_bytes}-byte limit; write large payloads "
                "directly to object storage with vended credentials instead of through the catalog"
            ),
        )
