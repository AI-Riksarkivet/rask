"""Optional OpenTelemetry tracing — no-op when OTEL is not installed.

Automatically initializes the OTEL SDK when ``OTEL_EXPORTER_OTLP_ENDPOINT``
is set (or when ``configure_tracing()`` is called explicitly).

Supports both ``grpc`` and ``http/protobuf`` protocols via the standard
``OTEL_EXPORTER_OTLP_PROTOCOL`` env var.

Usage::

    from rahcp_client.tracing import tracer

    with tracer.start_as_current_span("s3.upload") as span:
        span.set_attribute("s3.bucket", bucket)
        ...

If ``opentelemetry-api`` is not installed, ``tracer`` is a no-op.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

_initialized = False


def configure_tracing(
    service_name: str = "rahcp-client",
    endpoint: str | None = None,
    protocol: str | None = None,
) -> None:
    """Initialize the OTEL SDK with OTLP exporter.

    Called automatically if ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is set.
    Safe to call multiple times — only initializes once.

    Args:
        service_name: Service name in traces (default: rahcp-client).
        endpoint: OTLP endpoint. Defaults to ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.
        protocol: ``grpc`` or ``http/protobuf``. Defaults to
            ``OTEL_EXPORTER_OTLP_PROTOCOL`` env var, then ``http/protobuf``.
    """
    global _initialized  # noqa: PLW0603
    if _initialized or not OTEL_AVAILABLE:
        return

    otlp_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not otlp_endpoint:
        return

    otlp_protocol = protocol or os.environ.get(
        "OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"
    )

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
        }
    )
    provider = TracerProvider(resource=resource)

    try:
        if otlp_protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _initialized = True
        log.info("OTEL tracing enabled: %s (%s)", otlp_endpoint, otlp_protocol)
    except Exception:
        log.warning("Failed to initialize OTEL exporter", exc_info=True)


if OTEL_AVAILABLE:
    configure_tracing()
    tracer = trace.get_tracer("rahcp-client")
else:

    class _NoOpSpan:
        """Span that does nothing."""

        def set_attribute(self, key: str, value: Any) -> None:
            pass

        def set_status(self, *args: Any, **kwargs: Any) -> None:
            pass

        def record_exception(self, exc: BaseException) -> None:
            pass

    class _NoOpTracer:
        """Tracer that returns no-op spans."""

        @contextmanager
        def start_as_current_span(self, name: str, **kwargs: Any):  # noqa: ANN201
            yield _NoOpSpan()

    tracer = _NoOpTracer()  # type: ignore[assignment]
