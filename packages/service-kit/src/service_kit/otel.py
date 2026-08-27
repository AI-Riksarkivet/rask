"""OpenTelemetry wiring — opt-in OTLP/HTTP export for the fleet services.

No-op unless instrumentation is enabled (settings.otel_enabled) or an OTLP
endpoint is configured (OTEL_EXPORTER_OTLP_ENDPOINT). The OTLP exporter reads
OTEL_EXPORTER_OTLP_* from the environment (endpoint, protocol, headers), so this
module only constructs providers + instruments the app; it hardcodes no target.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from service_kit.config import Settings


if TYPE_CHECKING:
    from opentelemetry.trace import Span


#: Headers the request hook may copy onto a span, and nothing else.
#:
#: An ALLOWLIST rather than a denylist, because the reference's rule is absolute — "Don't put PII
#: (email, raw user id, auth tokens) in span attributes — those go to the trace backend forever" — and
#: a denylist is the wrong shape for a rule you cannot walk back. A header added upstream tomorrow is
#: excluded by default instead of leaking until someone notices.
_SPAN_HEADERS: dict[str, str] = {
    "x-request-id": "request.id",
    # The Dapr caller's app-id: which SERVICE invoked this one, which is attribution rather than
    # identity. The subject stays off the span deliberately.
    "dapr-caller-app-id": "rask.caller.app_id",
}


def server_request_hook(span: Span | None, scope: Mapping[str, Any]) -> None:
    """Join the estate's own `X-Request-ID` to the span it belongs to.

    `RequestIDMiddleware` mints the id, stores it on `request.state` and echoes it to the caller — and
    it reached no span and no log, so a caller holding an id from a failed request had nothing to
    search for. The correlation the header exists to provide did not exist.

    IN THE INSTRUMENTATION, not in another middleware, and that is the point rather than a convenience.
    viewer, search and annotator deliberately run no `BaseHTTPMiddleware` RequestID pair —
    `media/middleware.py` explains that `BaseHTTPMiddleware` fully buffers the response body, which
    would break the `/api/explorer` Range streaming that 206 video seeking depends on — and that same
    docstring names this seam as the remedy: "wire it via OpenTelemetry's ASGI instrumentation". A hook
    buffers nothing, so it restores correlation for the three apps that consciously traded it away.

    Defensive on both counts the reference's own example is: a missing header is normal (the hook runs
    on every request in every app), and a non-recording span must cost nothing.
    """
    if not (span and span.is_recording()):
        return
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or []
    for raw_key, raw_value in headers:
        attribute = _SPAN_HEADERS.get(raw_key.decode("latin-1").lower())
        if attribute and raw_value:
            span.set_attribute(attribute, raw_value.decode("latin-1"))


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

    import atexit

    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
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

    # Logs: THE THIRD SIGNAL, and the one this seam silently threw away. `LoggingInstrumentor` does
    # install an OTLP `LoggingHandler`, but with no `logger_provider` argument it binds to the global
    # `ProxyLoggerProvider`, whose `ProxyLogger` falls back to `_noop_logger`; the handler's `emit`
    # skips only on `NoOpLogger`, and a proxy is not one. So every record was translated into an OTel
    # record and then dropped — full cost, no delivery. The provider is passed EXPLICITLY below rather
    # than relying on `set_logger_provider` having run first, because that global is set-once and a
    # caller that lost the race would silently re-bind to the proxy.
    #
    # This is what emptied the compliance audit trail: `governed/audit.py` puts every field in
    # `extra={...}`, and the only formatter on that route (`__init__.py`) references no extra key, so an
    # `ingest_service_token` DENY reached storage as the bare line `... INFO lance.audit - audit`.
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        server_request_hook=server_request_hook,
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)
    LoggingInstrumentor().instrument(set_logging_format=True, logger_provider=logger_provider)

    # Shutdown hooks, because THE FLEET HAS NO LAUNCHER. `opentelemetry-instrument` registers these
    # itself; `command: ["uvicorn"]` does not, so all three batch processors would discard whatever sat
    # in the buffer at exit. That silently loses the most valuable window there is — the records emitted
    # in the seconds before a crash or a SIGTERM, which is precisely when someone goes looking.
    atexit.register(tracer_provider.shutdown)
    atexit.register(meter_provider.shutdown)
    atexit.register(logger_provider.shutdown)

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

    # botocore — THE OBJECT STORE. Every S3 call in the estate rode this uninstrumented: vending
    # (`catalog/core/vending.py`), the warehouse registry (`catalog/services/warehouses.py`), the
    # records plane (`service_kit/lakehouse/records.py`) and `storage/client.py`. boto3 is built on
    # botocore, so instrumenting the lower layer covers every client the estate constructs without
    # any of them changing.
    #
    # The failure this closes is the one the `requests` note above already names: an uninstrumented
    # EXPENSIVE leg beside instrumented cheap ones does not make the trace incomplete, it makes it
    # MISLEADING. A request that spent four seconds in S3 and one that spent none looked identical.
    with suppress(ImportError):
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument(tracer_provider=tracer_provider)

    # urllib3 — THE KUBERNETES LEG, and the one no other instrumentor reaches. The k8s client
    # (`controlplane/k8s.py`) speaks urllib3 directly rather than requests or httpx, so the
    # controlplane's whole reason for existing — reading Project CRs for the home picker — produced no
    # client span at all. It also catches any library that vendors urllib3 without going through
    # `requests`.
    with suppress(ImportError):
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

        URLLib3Instrumentor().instrument(tracer_provider=tracer_provider, meter_provider=meter_provider)

    return True
