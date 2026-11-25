"""A request the object store did not answer is answered 503 where the failure is caught, and raised no further.

Starlette runs an ``Exception`` handler inside ``ServerErrorMiddleware``, which sends the handler's answer and then raises
the exception again so the server can log it (starlette 1.10.0 ``middleware/errors.py``); uvicorn then logs it a second
time, as ``Exception in ASGI application`` at ERROR. A store that did not answer is an outage the service expects and
its caller retries, so it is answered here instead, by the app factory inside the request's trace: the spec's 503 with
one WARN record under the service's logger (:func:`lakehouse.problems.ns_errors.store_unavailable`), and nothing raised
on. Whether a failure is an outage is :func:`lakehouse.problems.outage.reads_as_store_outage`'s judgement, which reads any
exception, so no handler keyed on a class could catch them all. Any other failure, and an outage met once the answer has
begun, is raised on unchanged.
"""

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakehouse.problems.ns_errors import store_unavailable
from lakehouse.storage.outage import reads_as_store_outage


class StoreOutageMiddleware:
    """Answer a request the object store did not answer with the spec's 503, logged once under ``log``."""

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
        except Exception as exc:
            if started or not reads_as_store_outage(exc):
                raise
            answer = store_unavailable(exc, scope, log=self._log)
            await answer(scope, receive, send)
