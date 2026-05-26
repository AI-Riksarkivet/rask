---
name: otel
description: OpenTelemetry for Python services in this project — SDK setup, auto-instrumentation, custom spans/metrics/logs, semantic conventions, and the Collector pipeline that consumes the data. Use when instrumenting a Python service with traces/metrics/logs, picking attribute names, debugging missing telemetry, or deciding what belongs in the SDK vs the Collector.
---

# OpenTelemetry (Python-first)

The single reference for OTel in this project. Replaces the four upstream dash0 skills. Vendor-neutral — no backend-specific guidance.

Layered above `python-infrastructure` → `References/observability.md`, which pins the project-specific conventions (resource attributes, log severity, golden signals, NATS context propagation). Read that first if you're applying this in code; come here for the deep reference.

## Scope routing

| If you need to…                                                                                                          | Read                       |
| ------------------------------------------------------------------------------------------------------------------------ | -------------------------- |
| Install the Python SDK, auto-instrument, set env vars, custom spans, structured logs, graceful shutdown, troubleshooting | `references/python-sdk.md` |
| Name spans, pick span kind, set status, choose metric instrument types, structure log records, manage cardinality        | `references/signals.md`    |
| Look up the right attribute name, decide resource vs span placement, find a semconv namespace                            | `references/attributes.md` |
| Run a Collector pipeline, order processors, send to a backend over OTLP                                                  | `references/collector.md`  |

## Key principles

1. **Auto-instrument first, manual for business operations.** `opentelemetry-bootstrap -a install` covers FastAPI, httpx, asyncpg, redis, requests, SQLAlchemy and most libs we use. Add manual spans only for domain logic the framework can't see (`process_order`, `validate_invoice`).
2. **Three signals, one SDK.** Traces, metrics, and logs all go out via OTLP. Don't add `structlog` or a Prometheus client — the OTel Python SDK's stdlib-`logging` handler and metrics API cover both.
3. **Sample in the Collector, not the SDK.** Use the default `AlwaysOn` sampler in the SDK. Tail/head sampling lives in the Collector where the outcome of a request is known.
4. **Cardinality is the cost driver.** Metric attribute values must be bounded. `http.route` not `url.path`; `user.tier` not `user.id`. Per-request identifiers go on spans and logs, not metrics.
5. **Semconv names first, custom names last.** Search the [Attribute Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/) before inventing. Custom attributes use a reverse-DNS namespace (`com.rask.order.priority`).
6. **Set status `ERROR` only on final failure.** Retried-then-succeeded operations stay `UNSET`. Every `ERROR` carries a status message with the error class.
7. **Resource attributes identify the producer.** `service.name`, `service.version`, `deployment.environment.name`. Set via env (`OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`), never per-span.

## Quick start (Python)

```bash
uv add opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install

export OTEL_SERVICE_NAME="rask-api"
export OTEL_RESOURCE_ATTRIBUTES="service.version=1.2.3,deployment.environment.name=production"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317"
export OTEL_METRICS_EXPORTER="otlp"  # traces & logs default to otlp; metrics doesn't
export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED="true"

opentelemetry-instrument python -m rask.api
```

For development without a Collector, swap exporters to the console:

```bash
export OTEL_TRACES_EXPORTER=console OTEL_METRICS_EXPORTER=console OTEL_LOGS_EXPORTER=console
```

Full details in `references/python-sdk.md`.

## Top gotchas

- **Sync code inside an async handler blocks the loop.** Auto-instrumentation can't fix a `requests.get(...)` inside an async route — use `httpx.AsyncClient`. See sibling `python-infrastructure`.
- **Trace context is lost across queues.** Publishing to NATS JetStream drops the span unless you `propagate.inject(carrier)` into message headers and `propagate.extract` on the consumer. See `python-infrastructure/References/observability.md`.
- **`opentelemetry-instrument` is required.** Just installing the packages does nothing — the SDK isn't activated until the launcher runs. Missing instrumentations? Re-run `opentelemetry-bootstrap -a install` in the same venv.
- **No metrics by default.** `OTEL_METRICS_EXPORTER` defaults to `none` in Python (unlike traces/logs). Set it explicitly to `otlp`.
- **Span events for exceptions are deprecated.** Don't call `span.record_exception(e)` for new code. Emit a log record with `exception.type`/`exception.message`/`exception.stacktrace` inside the active span context — it'll carry `trace_id`/`span_id` automatically.
- **`opentelemetry-instrument` registers an `atexit` shutdown hook automatically.** No `atexit.register(provider.shutdown)` needed unless you set up the SDK programmatically.

## References

- [OpenTelemetry Python docs](https://opentelemetry.io/docs/languages/python/)
- [Attribute Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/)
- [Semantic Conventions spec](https://opentelemetry.io/docs/specs/semconv/)
- [OTel Collector docs](https://opentelemetry.io/docs/collector/)
- [Instrumentation Score spec](https://github.com/instrumentation-score/spec) — vendor-neutral quality score
