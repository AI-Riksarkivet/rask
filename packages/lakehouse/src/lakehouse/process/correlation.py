"""Correlation: the W3C trace context every request, log line, audit record, commit and event is joined by (ADR 0209).

One id joins them: the trace a request runs under. The app factory traces every request (`lakehouse.service_app.factory`), taking
the caller's ``traceparent`` where one is sent and starting a trace where none is, so a caller's own trace reaches every
record its request causes and a request with none still has one. Code that does not take ``request`` as a parameter reads
it here, from the active span: a log line through :class:`CorrelationFilter`, the audit trail through
:func:`current_trace_id`, a commit's ``rask.traceparent`` and a staged event's envelope through
:func:`current_traceparent`. Work done later on behalf of that request (a relay delivering a staged event, a reconcile
announcing a commit) runs inside :func:`continuing`, under the trace the record names, so the later work joins the
request's trace rather than starting one of its own.

The trace id is a correlation id, never evidence: a caller may send any well-formed ``traceparent``, and nothing grants
or attributes on it. The W3C grammar is what is held to, so a caller's text reaches no record unchecked.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Final, TypeIs

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


#: A ``traceparent`` as W3C Trace Context level 1 writes it, version ``00``: a 32-hex trace id and a 16-hex parent id,
#: neither all zeros, and the flags. Lower case only, as the specification requires of a sender.
_TRACEPARENT: Final = re.compile(r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}")
#: One ``tracestate`` list-member as W3C Trace Context level 1 writes it: a key (a tenant's ``id@system`` included) and a
#: value of printable characters with no ``,`` or ``=``. A header holds at most 32 of them.
_TRACESTATE_KEY: Final = r"(?:[a-z][_0-9a-z\-*/]{0,255}|[a-z0-9][_0-9a-z\-*/]{0,240}@[a-z][_0-9a-z\-*/]{0,13})"
_TRACESTATE_VALUE: Final = r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]"
_TRACESTATE_MEMBER: Final = re.compile(rf"[ \t]*{_TRACESTATE_KEY}={_TRACESTATE_VALUE}[ \t]*")
_TRACESTATE_MEMBERS: Final = 32
#: The one propagator these records are written and read with, whatever the process's global one is configured to: the
#: records are W3C Trace Context's own, and a deployment's choice of extra propagators (baggage, B3) adds nothing to them.
_PROPAGATOR: Final = TraceContextTextMapPropagator()
#: How an absent trace id renders on a log line and in an audit record: a visible placeholder, not a blank that reads
#: like a formatting bug.
NO_TRACE: Final = "-"


def is_traceparent(value: object) -> TypeIs[str]:
    """Whether ``value`` is a well-formed W3C ``traceparent`` (version ``00``) naming a real trace and span."""
    return isinstance(value, str) and _TRACEPARENT.fullmatch(value) is not None


def is_tracestate(value: object) -> TypeIs[str]:
    """Whether ``value`` is a well-formed W3C ``tracestate``: at most 32 list-members, each a key and a value."""
    if not isinstance(value, str):
        return False
    members = value.split(",")
    return len(members) <= _TRACESTATE_MEMBERS and all(_TRACESTATE_MEMBER.fullmatch(member) for member in members)


def extracted(fields: dict[str, str]) -> Context:
    """The trace context ``fields`` carry (``traceparent`` and ``tracestate``), read by the W3C propagator alone; the
    empty context for fields that carry none or a malformed one."""
    return _PROPAGATOR.extract(fields)


def current_trace_id() -> str | None:
    """The 32-hex id of the trace the caller runs under, or ``None`` outside one."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def current_trace_context() -> dict[str, str]:
    """The active span's W3C trace context as the fields that carry it, ``traceparent`` and (where set)
    ``tracestate``; empty outside a span."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier


def current_traceparent() -> str | None:
    """The active span's ``traceparent``, or ``None`` outside one."""
    return current_trace_context().get("traceparent")


@contextmanager
def continuing(traceparent: str | None, tracestate: str | None = None) -> Generator[None]:
    """Run the block under the trace ``traceparent`` names, so a span started in it is a child of that trace.

    A ``traceparent`` that is absent or not well formed leaves the block under whatever trace the caller runs in: a record
    that names no trace starts none, and a malformed one is never adopted. A ``tracestate`` that is not well formed is
    left out.
    """
    if not is_traceparent(traceparent):
        yield
        return
    carrier = {"traceparent": traceparent} | ({"tracestate": tracestate} if is_tracestate(tracestate) else {})
    token = otel_context.attach(extracted(carrier))
    try:
        yield
    finally:
        otel_context.detach(token)


class CorrelationFilter(logging.Filter):
    """Stamp ``record.trace_id`` on every record: the trace the active span runs under, or ``-`` outside one.

    A FILTER on the root handler, not a middleware: a middleware annotates only the records it writes itself, while a
    filter covers every module in every service, libraries included, through `setup_logging`'s single formatter. Read
    off the active span, never off `LoggingInstrumentor`'s ``otelTraceID``, so a process run without
    `opentelemetry-instrument` prints the same id the audit trail and the problem body's ``instance`` carry.

    Never raises and never drops a record: a correlation aid that could swallow a log line would be a far worse bug than
    the missing id it adds. Guaranteeing the attribute is what lets the formatter name it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or NO_TRACE
        return True


__all__ = [
    "NO_TRACE",
    "CorrelationFilter",
    "continuing",
    "current_trace_context",
    "current_trace_id",
    "current_traceparent",
    "extracted",
    "is_traceparent",
    "is_tracestate",
]
