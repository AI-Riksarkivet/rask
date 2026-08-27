"""Request-scoped correlation, for code that does not take ``request`` as a parameter.

`RequestIDMiddleware` minted an id, stored it on `request.state` and echoed `X-Request-ID` back — and
nothing read either. A caller could quote the id from a failed request and an operator had nothing to
grep for. This is the half that was missing: the id published somewhere a log record, a repository
method or a background helper can reach without plumbing `request` through every signature.

WHAT WAS NEVER BROKEN, so this is not sold as more than it is: correlation was already available.
`LoggingInstrumentor` injects `otelTraceID`/`otelSpanID` into every record and ships them to
GreptimeDB. What was dead was the REQUEST ID specifically — the value the estate hands the caller.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar


#: The current request's id. `"-"` rather than `""` so an absent value renders as a visible placeholder
#: in a log line instead of a blank that reads like a formatting bug.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    """The id for the request being served, or ``"-"`` outside one."""
    return request_id_ctx.get()


class RequestIdFilter(logging.Filter):
    """Stamp ``record.request_id`` on every record, from the context var.

    A FILTER, not a `LoggingMiddleware`, and the difference is the whole reason this reaches anything:
    a middleware can only annotate records it writes itself, while a filter on the root handler covers
    every module in every service — including libraries — through `setup_logging`'s single formatter,
    with no per-service edit.

    Never raises and never drops a record: a correlation aid that could swallow a log line would be a
    far worse bug than the missing id it fixes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True
