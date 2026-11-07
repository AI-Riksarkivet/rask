"""The process's log handler: every record to stdout, stamped with its request and trace ids.

`lakehouse.lance_app.build_lance_service_app` runs `setup_logging` before it builds an app, so every lakehouse
service logs through one handler and one format.
"""

import logging
import os
import sys

from lakehouse.context import CorrelationFilter, DiagnosticFormatter


def setup_logging() -> None:
    """Send application logs to stdout — configured on the ROOT logger, so every module inherits it.

    This used to attach the handler to two logger names, ``core`` and ``backends``, and its docstring
    said why: it "mirrors core.main". That monolith is gone. Every module in every service now uses
    ``logging.getLogger(__name__)`` — ``lineage.services.consumer``, ``maintenance.services.sweep``,
    ``catalog.api.v1.endpoints.tables`` — and NONE of those names sit under ``core.*`` or
    ``backends.*``. So the handler decorated two trees nobody logs to, every record propagated to a
    root logger with no handlers, and was discarded. Only uvicorn's own logs (which carry their own
    handlers) ever reached stdout, which is exactly why the estate looked quiet while it was not.

    Measured live 2026-08-17, and verified three times against the running service:

    * a malformed event POSTed to the running ingest answered ``{"status":"DROP"}`` — returned on the
      single line immediately AFTER ``log.error("lineage_event_invalid")``, so the branch demonstrably
      ran — and produced no log line at all. After this fix the identical request logs
      ``ERROR lineage.services.consumer — lineage_event_invalid``.

    The class this closes is the swallowed diagnostic — any handler that catches a failure by design
    and reports it with a WARNING instead. That warning had nowhere to go, so the one signal
    distinguishing "this is fine" from "this has been failing all week" did not exist. The estate has
    fewer of those than it did (lineage's feed write is now part of the ingest transaction and fails
    loudly rather than warning), but every best-effort arm that remains depends on this being fixed.

    ``RASK_LOG_LEVEL`` is the volume lever (default INFO). Root-level INFO does make chatty
    third-party libraries audible; that is the correct trade against losing every application log,
    and it is one env var to turn down rather than a hardcoded allowlist that goes stale the same way
    this one did.
    """
    level = os.environ.get("RASK_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent by handler NAME, not by count: the app factory runs this per app, and a process may build
    # several; a duplicate handler emits every line twice.
    if not any(h.get_name() == "rask-stdout" for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("rask-stdout")
        # THE REQUEST ID JOINS EVERY RECORD HERE. `RequestIDMiddleware` mints one and echoes it to the
        # caller; until this filter existed nothing read it, so a user could quote an id from a failed
        # request and an operator had nothing to grep for.
        #
        # A FILTER on the one handler, not a `LoggingMiddleware`: a middleware annotates only the
        # records it writes itself, while this covers every module in every service — libraries
        # included — with no per-service edit. Outside a request it renders `-`, which is why the
        # context var defaults to a visible placeholder rather than an empty string that would read
        # like a formatting bug.
        handler.addFilter(CorrelationFilter())
        # THE TRACE ID JOINS IT, and this is the tier that was missing one. `LoggingInstrumentor`
        # attaches `otelTraceID` to every record, but the only way it ever reached a LINE was
        # `set_logging_format=True` → `logging.basicConfig(format=...)`, which no-ops because this
        # handler already exists. So the id rode on every record and printed on none of them: the
        # OTLP copy in GreptimeDB could be joined to a trace and `kubectl logs` — the copy an
        # operator reads first — could not. Both fields come from the filter, so this format string
        # is safe on a record that never met OpenTelemetry.
        # `DiagnosticFormatter`, not `Formatter`: the correlation fields have slots in this string, and
        # everything the caller recorded in `extra=` follows the message. A format string cannot do the
        # second half — it names fixed fields, and the diagnostics differ at every call site.
        handler.setFormatter(DiagnosticFormatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] [%(trace_id)s] — %(message)s"))
        root.addHandler(handler)
