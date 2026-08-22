"""OpenTelemetry wiring — opt-in OTLP/HTTP export for the fleet services.

No-op unless instrumentation is enabled (settings.otel_enabled) or an OTLP
endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT). The OTLP exporter reads
OTEL_EXPORTER_OTLP_* from the environment (endpoint, protocol, headers), so this
module only constructs providers + instruments the app; it hardcodes no target.
"""

from __future__ import annotations

import os
from contextlib import suppress

from fastapi import FastAPI

from service_kit.config import Settings


def setup_otel(app: FastAPI, service_name: str, settings: Settings | None = None) -> bool:
    """Wire traces/metrics/logs OTLP export + FastAPI instrumentation.

    `settings` is optional: services built on `make_service_app` pass their
    `Settings`, but a bespoke entrypoint (e.g. the gateway, which uses plain
    env + no `Settings`) can omit it and rely on `OTEL_EXPORTER_OTLP_ENDPOINT`
    alone to opt in.

    Returns True if instrumentation was applied, False if skipped.

    **An explicit `Settings` DECIDES — the endpoint env is only a fallback.** This used to be
    `(settings is not None and settings.otel_enabled) or bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))`,
    an `or` that made ambient env an override nothing could turn off: a caller passing
    `RASK_OTEL_ENABLED=false` still got a live exporter whenever the variable happened to be set in
    the environment. "Off" that cannot be selected is not a setting.

    That is not theoretical, and the place it bites is the one place it must not: **`dagger call test`**.
    Dagger injects `OTEL_EXPORTER_OTLP_ENDPOINT` into every container it runs, for its own telemetry.
    So inside CI every app built by a test wired a real exporter aimed at Dagger's collector, which
    rejects application metrics (`unknown aggregation from pb` in the engine log) — and the SDK then
    retries with exponential backoff. The suite did not fail; it *slept*. Measured on this branch:
    `packages/service-kit/tests` took **33s at HEAD with 31 tests and 128s with 47** — ~2.7s per unit
    test, all of it backoff — and a full run sat in `hrtimer_nanosleep` at ~1.7% CPU for the best part
    of an hour. `test_setup_otel_noop_when_disabled` has been asserting exactly this and failing on
    main; it was reporting a real defect, not being wrong.

    The fallback is preserved for the case that motivated it: `services/gateway` calls this with no
    `Settings` at all and opts in through the endpoint alone. Production is unaffected either way —
    `chart/templates/_helpers.tpl` renders `RASK_OTEL_ENABLED: "true"` and `OTEL_EXPORTER_OTLP_ENDPOINT`
    from the SAME `observability.enabled` guard, so the two can never disagree there.
    """
    enabled = settings.otel_enabled if settings is not None else bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
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

    # `requests`, because HTTPX is not the only client in the estate and the uninstrumented half is
    # the EXPENSIVE half. Ray's `JobSubmissionClient` performs every call through `requests`, which
    # covers `list_jobs` — the call that measured 164.7 MB / 81,155 jobs and OOM-killed the compute
    # pod — and the pruner's one-HTTP-DELETE-per-job loop. Without this the trace view is not merely
    # incomplete but MISLEADING: the cheap httpx reads carry client spans while the costly requests
    # calls appear instantaneous, so a twenty-minute prune pass and a two-second one look identical.
    #
    # Guarded: the fleet's storeless services do not all pull `requests` in, and a missing optional
    # instrumentor must degrade to "no client spans", never to a service that cannot start.
    with suppress(ImportError):
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)

    # gRPC — THE DAPR LEG. Now that `lance-tracing` names an exporter the sidecars emit spans, but
    # without this the app->sidecar call carries no `traceparent`, so the sidecar's span ROOTS A NEW
    # TRACE. That fresh id is stamped into the CloudEvent envelope and persisted as
    # `ExecutionStartedEvent.ParentTraceContext`, so every activity, lineage event and notification
    # downstream inherits the orphan: a severed subtree, not a missing span, presenting as a sampling
    # problem rather than a missing instrumentor.
    #
    # BOTH VARIANTS, and installing one is worse than installing neither because it looks configured:
    # `dapr.aio.clients.DaprClient` builds `grpc.aio` channels (publish, state, bindings, workflow
    # schedule) while `dapr-ext-workflow`'s `DaprWorkflowClient` uses SYNC grpc, and the two
    # instrumentors patch different symbols. Dapr accepts the W3C `traceparent` metadata OTel injects
    # via its documented `grpc-trace-bin` fallback, so no app-side header plumbing is needed.
    #
    # `BaseInstrumentor` is a per-class singleton, so this is safe alongside `dapr.ext.workflow`'s own
    # import-time `GrpcInstrumentorClient().instrument()`: the second call logs "already instrumented"
    # and returns.
    with suppress(ImportError):
        from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient, GrpcInstrumentorClient

        GrpcAioInstrumentorClient().instrument(tracer_provider=tracer_provider)
        GrpcInstrumentorClient().instrument(tracer_provider=tracer_provider)

    # aiohttp — the transport behind EVERY actor call and EVERY authorization check. `ActorProxy`
    # resolves to `DaprActorHttpClient`, and `ActorProxyFactory` exposes no headers_callback, so there
    # is no app-side hook to add context by hand; the `openfga_sdk` rides the same library, which is
    # why the authz hot path had no span, no metric and no latency anywhere.
    with suppress(ImportError):
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor

        AioHttpClientInstrumentor().instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)
    return True
