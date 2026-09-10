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


# WHAT A RECORD CARRIES THAT NOBODY DELIBERATELY RECORDED. Derived from a real `LogRecord` rather than
# typed out, so a stdlib that grows a field does not start printing it as a diagnostic; `asctime` and
# `message` join it because the FORMATTER sets those, not `__init__`.
#
# The rest are attributes that ride every record from somewhere other than the caller:
# `LoggingInstrumentor` stamps the four `otel*` fields on all of them (`trace_id` above is already
# derived from one, and the format string prints it), the filter's own two have their own slots, and
# uvicorn's `color_message` is an ANSI copy of the message meant for uvicorn's formatter to substitute
# (`uvicorn/logging.py:61`) — rendering it prints every startup line in the fleet twice.
#
# PRIVATE NAMES GO WITH THEM, and a library in this estate is why rather than a principle: importing Ray
# installs a log record factory that stamps `_ray_timestamp_ns` — a nanosecond integer — on EVERY record
# in the process (`ray/_private/log.py:71-88`), so the compute service and the whole Ray lane would have
# grown that field on every line. Naming it would fix this release and miss the next field it adds;
# leading-underscore is the convention a library reaches for when it means "mine, not the caller's", and
# no diagnostic in `services/` or `packages/` is named that way.
_AMBIENT_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "asctime",
    "message",
    "request_id",
    "trace_id",
    "otelTraceID",
    "otelSpanID",
    "otelServiceName",
    "otelTraceSampled",
    "color_message",
}


class DiagnosticFormatter(logging.Formatter):
    """Render the fields a caller passed in ``extra=`` after the message.

    THE ESTATE RECORDS CAUSES IN ``extra=`` — 626 call sites, measured 2026-09-10 — and this tier
    printed the event name alone. `lineage_outbox_event_stranded` names the strand and puts the reason
    in `extra`; on stdout it read as a bare event with no cause, and the cause had to be recovered by
    reading the handler's source. The OTLP copy in GreptimeDB carried it the whole time, which is what
    made the gap easy to miss: the diagnostic was not lost, only absent from the copy an operator
    reaches for first.

    ``repr`` rather than ``str`` for the values: the commonest diagnostic in the estate is
    ``error=str(exc)``, and an unquoted exception string runs straight into the next field — or, when
    it spans lines, into what looks like a separate log record.

    NOTHING IS TRUNCATED, and that is a decision rather than an oversight. `lineage_reconcile_storage_loss`
    records `datasets=[...]` — a list that can hold every dataset in the estate — so this tail can emit a
    multi-kilobyte line. A cap would silently drop entries, which is the exact defect this class of fix
    exists to end, and the formatter cannot know which of 626 call sites can afford it. A call site whose
    diagnostic is genuinely unbounded should bound it there, where the meaning is known; the OTLP copy
    already carries the whole value either way, so a cap here would only make the two tiers disagree.

    Secrets are the standing hazard of a tail like this, and the rule that keeps them out is the same
    one that governs `extra=` itself: a secret's NAME may be recorded, never its value. Audited at the
    time of writing across every `extra=` in `services/` and `packages/` — the only credential-shaped
    keys are `{"secret": secret}` in the viewer and ingest object-store paths, both of which hold the
    Dapr secret's name. `services/maintenance/tests/test_the_rewrite_is_signed_by_a_scoped_credential.py`
    asserts on the whole record for exactly this reason.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        diagnostics = " ".join(f"{name}={value!r}" for name, value in vars(record).items() if not name.startswith("_") and name not in _AMBIENT_RECORD_FIELDS)
        return f"{line} {diagnostics}" if diagnostics else line
