"""The process's logs: one level, read from ``RASK_LOG_LEVEL``, and one handler that writes every record to stdout stamped
with the trace id it was written under.

Every lakehouse process runs `setup_logging` before it does anything else: the catalog, lineage and the maintenance
worker through `lakehouse.service_app.factory.build_service_app`, the issuer mirror from its app factory and the OpenFGA
model Job from its entry point. So each logs through one handler, one format and one level.

The handler sits on the ROOT logger. Every module logs through ``logging.getLogger(__name__)``, and a record that reaches
no handler is discarded without a trace, so a handler on any narrower name would leave a swallowed diagnostic (a failure
caught by design and reported with a WARNING) with nowhere to go. The OTLP log copy rides the root too:
``opentelemetry-instrument`` attaches the SDK's ``LoggingHandler`` there at process start, and a record reaches it only
when its own logger is enabled for the record's level. So the lakehouse's own loggers are set to the level the setting
names, and at the default (INFO) the audit and lifecycle tier (``access_denied``, ``openfga_provisioned``,
``maintenance_sweep``, ``warehouse_created``) reaches stdout and the OTLP copy alike.

The root takes the same level but never one below INFO. Under ``DEBUG`` the lakehouse's own DEBUG records still reach the
root's handlers (propagation consults a handler's level, not an ancestor logger's), while a library's DEBUG stays shut:
botocore's DEBUG writes each request's headers, a session token among them, and a log line is no place for a credential.
The compliance trail is armed apart from all of this (`lakehouse.governed.audit.configure_audit`), so a quieter level
cannot silence it.
"""

import logging
import sys
from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.process.correlation import CorrelationFilter


#: The lakehouse's own packages, whose module loggers follow the level. A client of the lakehouse configures its own
#: logging: naming one here would put a client's name in a shared seam.
_LAKEHOUSE_LOGGERS: Final = ("catalog", "lineage", "maintenance", "lakehouse")
_HANDLER_NAME: Final = "rask-stdout"
_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)s [%(trace_id)s] — %(message)s"

# What a record carries that nobody deliberately recorded. Derived from a real `LogRecord` rather than typed out, so a
# stdlib that grows a field does not start printing it as a diagnostic; `asctime` and `message` join it because the
# formatter sets those, not `__init__`. The rest ride every record from somewhere other than the caller:
# `LoggingInstrumentor` stamps the four `otel*` fields, the correlation filter's field has its own slot, and uvicorn's
# `color_message` is an ANSI copy of the message meant for uvicorn's own formatter (`uvicorn/logging.py:61`), which would
# print every startup line twice.
_AMBIENT_RECORD_FIELDS: Final = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "asctime",
    "message",
    "trace_id",
    "otelTraceID",
    "otelSpanID",
    "otelServiceName",
    "otelTraceSampled",
    "color_message",
}


class LogSettings(BaseSettings):
    """The process's log level, as the chart renders it to every lakehouse pod (``logLevel``)."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO", alias="RASK_LOG_LEVEL")

    @field_validator("level", mode="before")
    @classmethod
    def _any_case(cls, value: object) -> object:
        """Take a level in any case, as an operator types it."""
        return value.upper() if isinstance(value, str) else value


class DiagnosticFormatter(logging.Formatter):
    """Render the fields a caller passed in ``extra=`` after the message.

    The estate records a record's cause in ``extra=`` (626 call sites, measured 2026-09-10): a line that printed the
    event name alone would hand an operator a bare event with no cause, while the OTLP copy carried it all along.

    ``repr`` rather than ``str`` for the values: the commonest diagnostic is ``error=str(exc)``, and an unquoted
    exception string runs straight into the next field, or, when it spans lines, into what looks like another record.

    Nothing is truncated: a cap would silently drop entries (`lineage_reconcile_storage_loss` records every dataset it
    names), and only a call site knows whether its diagnostic can be bounded, so an unbounded one is bounded there. The
    OTLP copy carries the whole value either way, so a cap here would only make the two tiers disagree.

    A secret's NAME may be recorded in ``extra=``, never its value, the rule that keeps a tail like this free of
    credentials; `services/maintenance/tests/test_the_rewrite_is_signed_by_a_scoped_credential.py` asserts on the
    whole record for that reason.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        diagnostics = " ".join(f"{name}={value!r}" for name, value in vars(record).items() if name not in _AMBIENT_RECORD_FIELDS)
        return f"{line} {diagnostics}" if diagnostics else line


def setup_logging() -> None:
    """Set the process's log level from ``RASK_LOG_LEVEL`` and attach the one stdout handler to the root logger.

    Idempotent by the handler's name, not by a count of handlers: the app factory runs this per app, a process may build
    several, and a second handler would write every line twice.

    Raises:
        pydantic.ValidationError: ``RASK_LOG_LEVEL`` names no level, so the process does not start at one nobody chose.
    """
    level = logging.getLevelNamesMapping()[LogSettings().level]
    for name in _LAKEHOUSE_LOGGERS:
        logging.getLogger(name).setLevel(level)
    root = logging.getLogger()
    root.setLevel(max(level, logging.INFO))
    if not any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        root.addHandler(_stdout_handler())


def _stdout_handler() -> logging.Handler:
    """The stdout handler: every record stamped with its trace id, its ``extra=`` fields after the message."""
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    # The trace id is the one correlation id (ADR 0209): the id a problem body's `instance` gives a caller, the audit
    # trail's `audit.trace_id`, and the trace the OTLP copy is joined by. A filter on the one handler covers every module
    # in every service, libraries included; outside a trace it renders `-`, so the format is safe on any record.
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(DiagnosticFormatter(_FORMAT))
    return handler


__all__ = ["DiagnosticFormatter", "LogSettings", "setup_logging"]
