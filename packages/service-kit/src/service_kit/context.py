"""Request-scoped correlation, for code that does not take ``request`` as a parameter.

`RequestIDMiddleware` minted an id, stored it on `request.state` and echoed `X-Request-ID` back — and
nothing read either. A caller could quote the id from a failed request and an operator had nothing to
grep for. This is the half that was missing: the id published somewhere a log record, a repository
method or a background helper can reach without plumbing `request` through every signature.

WHAT WAS NEVER BROKEN, so this is not sold as more than it is: correlation was already available on
the OTLP tier. `LoggingInstrumentor` injects `otelTraceID`/`otelSpanID` into every record and ships
them to GreptimeDB. What was dead was the REQUEST ID specifically — the value the estate hands the
caller — and, separately, the TRACE ID on the stdout tier: the instrumentor attached it to every
record and `setup_logging`'s formatter never printed it, so the copy an operator reads first
(`kubectl logs`) was the one copy with no way back to a trace.
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


#: What `LoggingInstrumentor` writes when there is no active span — `"0"` before it has seen one, and
#: an all-zero id for an invalid span context. Neither is a trace anyone can look up, so both render
#: as the same visible placeholder rather than 32 zeros that read like a real id.
_ABSENT_TRACE_IDS = frozenset({"", "0", "0" * 32})


class CorrelationFilter(logging.Filter):
    """Stamp ``record.request_id`` and ``record.trace_id`` on every record.

    RENAMED from `RequestIdFilter` when it took on the second field: a class that stamps a trace id
    while calling itself the request-id filter is the stale naming this codebase keeps paying for.

    A FILTER, not a `LoggingMiddleware`, and the difference is the whole reason this reaches anything:
    a middleware can only annotate records it writes itself, while a filter on the root handler covers
    every module in every service — including libraries — through `setup_logging`'s single formatter,
    with no per-service edit.

    Never raises and never drops a record: a correlation aid that could swallow a log line would be a
    far worse bug than the missing id it fixes. That is also why `trace_id` is DERIVED here rather
    than named directly in the formatter — `logging` raises on a format field the record lacks, and a
    record with no `otelTraceID` is the normal case in dev, in tests, and in any service where
    `setup_otel` returned False. Guaranteeing the attribute is what lets the formatter reference it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        trace_id = str(getattr(record, "otelTraceID", "") or "")
        record.trace_id = "-" if trace_id in _ABSENT_TRACE_IDS else trace_id
        return True
