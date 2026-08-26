"""Request body-size limit — a pure-ASGI middleware that rejects oversized bodies with 413.

MOVED HERE FROM `catalog/api/body_limit.py` (2026-08-26, owner ruling). It was written for the
catalog and applied to the catalog alone, while five other apps built by `make_service_app` — the
GATEWAY among them, which buffers every proxied body whole and is the one app published at the edge —
got CORS + RequestID + Timing and no ceiling at all. `register_middleware` now installs it for every
service, so one cannot be added without a cap and a new route cannot arrive uncapped.

The shared default is a DoS CEILING, not a business rule: it exists to stop a multi-GB body reaching a
buffer, not to say what a payload should be. A service needing more sets its own — the catalog keeps
256 MiB explicitly, because Arrow-IPC writes are exactly the case this was built for.

The Arrow-IPC write endpoints (``/v1/table/{id}/create|insert|merge_insert``) read the whole request body
into memory before the handler runs, so a single unbounded POST — e.g. a multi-GB media blob pushed through
the catalog instead of the direct storage/vending path — OOMs the worker. This enforces the cap *before* the
body is buffered, two ways: a fast ``Content-Length`` reject, and a streaming byte counter that catches a
chunked or length-omitting client. Oversized requests get a problem+json 413 steering them to the direct path.

Pure ASGI (not ``BaseHTTPMiddleware``) so the cap applies as bytes arrive, independent of how the endpoint
reads the body — and so it composes below the app's exception handlers without buffering the request itself.
"""

from __future__ import annotations

import json

from lance_namespace import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from service_kit.lakehouse.ns_errors import problem_body


_PROBLEM_JSON = b"application/problem+json"


class _BodyTooLarge(Exception):
    """Raised from the wrapped ``receive`` when the streamed body exceeds the cap."""


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

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:  # chunked / lying Content-Length caught here
                    raise _BodyTooLarge
            return message

        try:
            await self._app(scope, limited_receive, send)
        except* _BodyTooLarge:
            # The endpoint reads the full body before sending a response, so no bytes have gone out yet.
            # ``except*`` (not ``except``) because an inner BaseHTTPMiddleware (e.g. maintenance_middleware)
            # runs the app in an anyio task group, which wraps the raise in an ExceptionGroup a bare
            # ``except`` would miss — turning the intended 413 into a 500. except* matches grouped AND naked.
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        # Through the SHARED builder, so this envelope carries the spec's required `code` and `error`.
        # A generated Lance-Namespace client validates `ErrorResponse` with `code` as a REQUIRED,
        # no-default field, so the four-key body this used to emit made the client raise rather than
        # read the refusal. This middleware cannot raise its way to the handler — it is pure ASGI and
        # must answer before the body is buffered — so the shape comes from `ns_errors` instead.
        body = json.dumps(
            problem_body(
                ErrorCode.INVALID_INPUT,
                status=413,
                title="Payload Too Large",
                slug="payload-too-large",
                detail=(
                    f"request body exceeds the {self._max_bytes}-byte limit; write large payloads "
                    "directly to object storage with vended credentials instead of through the catalog"
                ),
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", _PROBLEM_JSON),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
