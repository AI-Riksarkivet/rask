"""The request-id layer every lakehouse service carries (`lakehouse.lance_app`).

One id per request, minted or echoed, published to the context var `setup_logging`'s filter reads, so a caller can
quote an id from a failed request and an operator can grep for it.
"""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.context import request_id_ctx


_REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Echo or generate `X-Request-ID` per request; publish it to `request.state` and the context var.

    Inbound `X-Request-ID` is preserved so a reverse proxy / client can correlate logs across hops.
    Without it, we mint a UUID hex.

    PURE ASGI, NOT `BaseHTTPMiddleware`, and that is what makes it safe everywhere. `BaseHTTPMiddleware`
    fully buffers the response body, which would break a streamed body such as a 206 Range response; the
    catalog's Arrow-IPC data plane has the same shape. Because this one rewrites only the response START
    message and passes the body through untouched, no streaming route needs an exemption and every
    service can carry one id.

    It touches only the response START message to add the header; body chunks pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get(_REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        # `scope["state"]` is what `request.state` reads, so existing `request.state.request_id`
        # consumers keep working without knowing this middleware changed shape.
        scope.setdefault("state", {})["request_id"] = request_id

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                raw.append((_REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        # PUBLISHED TO THE CONTEXT, so code that does not take `request` — a log record, a repository
        # method, a background helper — can read it without plumbing. Before this the id was minted,
        # echoed and read by nothing: a caller could quote it and an operator had nothing to grep.
        token = request_id_ctx.set(request_id)
        try:
            await self.app(scope, receive, _send)
        finally:
            # THE RESET IS THE LOAD-BEARING HALF. Without it the value survives the response on a
            # reused worker and labels the NEXT request with the previous caller's id — worse than no
            # id at all, because it correlates the wrong things while looking correct.
            request_id_ctx.reset(token)
