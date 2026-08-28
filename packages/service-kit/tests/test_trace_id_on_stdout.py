"""The stdout tier is the copy an operator reads first, and it carried no trace id.

open_fastapi-audit — "`setup_otel` passes `logger_provider=` to `LoggingInstrumentor`, which the
installed 0.65b0 ignores — and `set_logging_format=True` is a no-op, so fleet stdout carries no trace
id".

THREE LEGS WERE FILED AND ONLY THE MIDDLE ONE IS A DEFECT, which the finding says itself. Read in the
installed `LoggingInstrumentor._instrument`:

* **`logger_provider=` really is dropped** — the method calls `get_logger_provider()` and never reads
  that kwarg. But `set_logger_provider(logger_provider)` runs two lines above the instrument call, so
  the global IS the provider we built. The bug is a FALSE COMMENT claiming a race protection that does
  not exist, not a behavioural fault. Fixed by deleting the sentence and asserting the binding.
* **`set_logging_format=True` is the real defect.** It reaches `logging.basicConfig(format=...)`,
  which NO-OPS when the root logger already has a handler — and `setup_logging` installed the named
  `rask-stdout` handler long before. So the record factory attaches `otelTraceID` to every record and
  the one formatter that exists never prints it.
* **The `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED` leg is refuted by a recorded decision** and
  is not touched here.

Blast radius is narrow and the finding is careful about it: the OTLP copy of every log already carries
trace/span context, so only the raw `kubectl logs` tier was blind.

THE FORMATTER MUST NOT DEPEND ON OTEL BEING ON. A formatter naming `%(otelTraceID)s` raises on any
record that lacks the attribute — which is every record in dev, in tests, and in any service where
`setup_otel` returned False. That would turn a correlation aid into a crash, so the attribute is
guaranteed by the same filter that already guarantees `request_id`.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from service_kit import setup_logging


@pytest.fixture
def stdout_lines() -> Iterator[io.StringIO]:
    """The real `rask-stdout` handler, redirected so the FORMATTED line can be read.

    Asserted on the formatted output rather than on the record, because the whole defect is that the
    attribute was present on the record and absent from the line.
    """
    root = logging.getLogger()
    previous = list(root.handlers)
    for handler in previous:
        root.removeHandler(handler)
    setup_logging()
    handler = next(h for h in root.handlers if h.get_name() == "rask-stdout")
    # Narrowed, not asserted away: `setup_logging` builds a StreamHandler, and this fixture only works
    # because it is one — swapping the stream is how the FORMATTED line becomes readable.
    assert isinstance(handler, logging.StreamHandler)
    buffer = io.StringIO()
    original_stream = handler.stream
    handler.setStream(buffer)
    try:
        yield buffer
    finally:
        handler.setStream(original_stream)
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in previous:
            root.addHandler(h)


@pytest.fixture
def instrumented() -> Iterator[None]:
    """`LoggingInstrumentor` active, as `setup_otel` leaves it in a live service."""
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    instrumentor = LoggingInstrumentor()
    already = instrumentor.is_instrumented_by_opentelemetry
    if already:
        instrumentor.uninstrument()
    instrumentor.instrument(inject_trace_context=True)
    try:
        yield
    finally:
        instrumentor.uninstrument()


def test_a_record_written_inside_a_span_PRINTS_its_trace_id(stdout_lines: io.StringIO, instrumented: None) -> None:
    """The finding: the id rides on the record and never reaches the line an operator greps."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("unit") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        logging.getLogger("lineage.services.consumer").error("lineage_event_invalid")

    line = stdout_lines.getvalue()
    assert expected in line, f"the stdout line carries no trace id, so an operator holding one from a dashboard cannot grep for it: {line!r}"


def test_a_record_written_OUTSIDE_a_span_still_formats(stdout_lines: io.StringIO, instrumented: None) -> None:
    """Outside a span the instrumentor sets the id to '0'. Printing `[0]` on every line is noise; the
    line must render a visible placeholder the way `request_id` already does."""
    logging.getLogger("catalog.api.v1.endpoints.tables").warning("no span here")

    line = stdout_lines.getvalue()
    assert "no span here" in line, f"the record never reached stdout: {line!r}"
    assert "0" * 32 not in line, f"an all-zero trace id was printed as though it were real: {line!r}"


def test_the_formatter_SURVIVES_otel_never_having_run(stdout_lines: io.StringIO) -> None:
    """No `instrumented` fixture — no `otelTraceID` on the record at all.

    This is the failure mode a naive `%(otelTraceID)s` introduces: `logging` raises on a missing
    attribute and the handler prints a traceback instead of the line. Dev, tests, and every service
    where `setup_otel` returned False are all in this state.
    """
    logging.getLogger("medallion.services.transform").info("otel is off")

    line = stdout_lines.getvalue()
    assert "otel is off" in line, f"the record was lost when otel was disabled: {line!r}"
    assert "Traceback" not in line and "--- Logging error ---" not in line, f"the formatter raised on a record with no otel attributes: {line!r}"


def test_setup_otel_BINDS_the_logger_provider_it_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LoggingInstrumentor` reads the GLOBAL provider, never the kwarg — so the binding is the
    contract, and the comment claiming the kwarg protects against a lost race is false.

    Asserted rather than commented: if `set_logger_provider` ever stops taking effect, every log
    record is translated to an OTel record and dropped by the proxy's no-op logger — full cost, no
    delivery, and no error anywhere.
    """
    from fastapi import FastAPI
    from opentelemetry._logs import get_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider

    from service_kit import setup_otel

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    assert setup_otel(FastAPI(), "svc-trace-binding") is True

    provider = get_logger_provider()
    assert isinstance(provider, LoggerProvider), (
        f"the global logger provider is {type(provider).__name__}, not the SDK one — `LoggingInstrumentor` "
        "would bind its handler to a proxy whose logger drops every record silently"
    )
