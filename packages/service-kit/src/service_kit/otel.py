"""OpenTelemetry wiring — opt-in OTLP/HTTP export for the fleet services.

No-op unless instrumentation is enabled (settings.otel_enabled) or an OTLP
endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT). The OTLP exporter reads
OTEL_EXPORTER_OTLP_* from the environment (endpoint, protocol, headers), so this
module only constructs providers + instruments the app; it hardcodes no target.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from service_kit.config import Settings


def setup_otel(app: FastAPI, service_name: str, settings: Settings | None = None) -> bool:
    """Wire traces/metrics/logs OTLP export + FastAPI instrumentation.

    `settings` is optional: services built on `make_service_app` pass their
    `Settings`, but a bespoke entrypoint (e.g. the gateway, which uses plain
    env + no `Settings`) can omit it and rely on `OTEL_EXPORTER_OTLP_ENDPOINT`
    alone to opt in.

    Returns True if instrumentation was applied, False if skipped.
    """
    enabled = (settings is not None and settings.otel_enabled) or bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    if not enabled:
        return False

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service_name})

    # Traces: BatchSpanProcessor -> OTLP/HTTP. The span exporter reads
    # OTEL_EXPORTER_OTLP_TRACES_HEADERS (carrying GreptimeDB's required
    # x-greptime-pipeline-name=greptime_trace_v1) from the environment.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Metrics: periodic OTLP/HTTP push. The FastAPI/HTTPX instrumentors emit RED
    # metrics (http.server.* request count/duration, http.client.*) automatically
    # once a MeterProvider is registered — no per-endpoint code. The metric
    # exporter must NOT carry the trace pipeline header, so it relies on the
    # generic OTEL_EXPORTER_OTLP_HEADERS (db-name only); GreptimeDB ingests OTLP
    # metrics at /v1/otlp/v1/metrics with no pipeline.
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider, meter_provider=meter_provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)
    LoggingInstrumentor().instrument(set_logging_format=True)
    return True
