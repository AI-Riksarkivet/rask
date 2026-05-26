# Observability

This project uses **OpenTelemetry** (OTLP) for the three signals — traces, metrics, logs — exported to an OTel Collector that fans out to backends. Don't pull in `structlog`, Prometheus client libs, or ad-hoc tracing — OTel's stdlib-`logging` handler, metrics API, and tracer cover all three.

## Contents

- Where to look first
- Required resource attributes
- Logging — stdlib `logging`, not structlog
- What to instrument
- The four golden signals
- Bounded cardinality
- Trace context across queue boundaries
- Cross-cutting decorator (timed + traced)
- Summary

## Where to look first

| If you need to…                                                                              | Read                                                  |
| -------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Set up the OTel SDK, configure exporters, instrument a service (manual + auto), troubleshoot | sibling skill **`otel`** → `references/python-sdk.md` |
| Design a Collector pipeline (receivers, processors, exporters)                               | sibling skill **`otel`** → `references/collector.md`  |
| Pick attribute names/values that match the spec (HTTP, DB, messaging, etc.)                  | sibling skill **`otel`** → `references/attributes.md` |
| Span naming/kind/status, metric instrument types, log structure                              | sibling skill **`otel`** → `references/signals.md`    |

This file documents the **project-specific conventions** layered on top of the `otel` skill. Read that for the deep reference.

## Required resource attributes

Every service sets these on `Resource.create({...})` at startup:

| Key                      | Value                                            | Source                   |
| ------------------------ | ------------------------------------------------ | ------------------------ |
| `service.name`           | The service name, e.g. `rask-api`, `rask-worker` | env: `OTEL_SERVICE_NAME` |
| `service.version`        | App version                                      | env: `SERVICE_VERSION`   |
| `deployment.environment` | `local` / `staging` / `production`               | env: `ENVIRONMENT`       |

Set via env or in code:

```bash
export OTEL_SERVICE_NAME="rask-api"
export OTEL_RESOURCE_ATTRIBUTES="service.version=1.2.3,deployment.environment=production"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4317"
export OTEL_TRACES_EXPORTER="otlp"
export OTEL_METRICS_EXPORTER="otlp"
export OTEL_LOGS_EXPORTER="otlp"
export OTEL_TRACES_SAMPLER="parentbased_traceidratio"
export OTEL_TRACES_SAMPLER_ARG="0.1"
export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED="true"
```

Prefer the env-var route; only set things in code that can't be expressed in env.

## Logging — stdlib `logging`, not structlog

Use stdlib `logging`. With `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true` (or the `LoggingInstrumentor`), records are emitted as OTel log records and carry the current trace/span IDs automatically.

```python
import logging

log = logging.getLogger(__name__)

# Pass structured fields via `extra=`; they end up as log-record attributes
log.info("order_processed", extra={"order_id": order.id, "amount": order.total})
log.warning("rate_limit_approaching", extra={"current_rate": 950, "limit": 1000})
log.error("payment_failed", extra={"order_id": order.id, "provider": "stripe"}, exc_info=True)
```

**Log-level discipline:**

| Level     | Use for                       | Examples                          |
| --------- | ----------------------------- | --------------------------------- |
| `DEBUG`   | Development diagnostics       | Variable values, internal state   |
| `INFO`    | Request lifecycle, operations | Request start/end, job completion |
| `WARNING` | Recoverable anomalies         | Retry attempts, fallback used     |
| `ERROR`   | Failures needing attention    | Exceptions, service unavailable   |

Never log expected behavior at `ERROR`. A user typing the wrong password is `INFO`, not `ERROR`.

## What to instrument

**Auto-instrument** what you can — `opentelemetry-bootstrap -a install`, then run with `opentelemetry-instrument`. This covers FastAPI, httpx, asyncpg, redis, requests, and most other libs we use, for free.

**Manually add spans** for business operations the auto-instrumentation can't see:

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def process_order(order_id: str) -> Order:
    with tracer.start_as_current_span("process_order") as span:
        span.set_attribute("order.id", order_id)

        with tracer.start_as_current_span("validate_order"):
            await validate_order(order_id)

        with tracer.start_as_current_span("charge_payment"):
            await charge_payment(order_id)

        return await load_order(order_id)
```

See the `otel` skill for span events, exception recording, semantic-convention attributes, and the full API.

## The four golden signals

For every external boundary (HTTP, DB, queue, cache), make sure you can answer:

1. **Latency** — request duration. Already on auto-instrumented spans; for custom work, record a `Histogram`.
2. **Traffic** — request rate. Auto-instrumented as span counts; or a `Counter` for custom flows.
3. **Errors** — error rate. Spans with `StatusCode.ERROR`; or a labeled `Counter`.
4. **Saturation** — pool/queue depth, CPU, memory. `UpDownCounter` for pools, `ObservableGauge` for resource usage.

```python
from opentelemetry import metrics

meter = metrics.get_meter(__name__)

request_duration = meter.create_histogram(
    name="http.server.request.duration",
    unit="s",
    description="Request latency",
)
active_connections = meter.create_up_down_counter(
    name="db.client.connections.usage",
    description="Connections currently in use",
)
```

Use **semantic-convention names** wherever they exist. See `otel` → `references/attributes.md`.

## Bounded cardinality

Never use unbounded values as metric attributes — user IDs, request IDs, raw paths with IDs.

```python
# BAD: explodes label cardinality
request_counter.add(1, {"http.method": "GET", "user.id": user.id})

# GOOD: bounded values only
request_counter.add(1, {"http.method": "GET", "http.route": "/users/{id}", "http.status_code": 200})
```

Per-user details belong in spans/logs (where they're indexed but not multiplied), not in metric labels.

## Trace context across queue boundaries

Publishing to NATS JetStream (or any queue) loses the current trace unless you propagate it explicitly via message headers:

```python
from opentelemetry import propagate

# Publisher
carrier: dict[str, str] = {}
propagate.inject(carrier)  # writes traceparent + tracestate keys
await js.publish(subject, payload, headers=carrier)

# Consumer
ctx = propagate.extract(dict(msg.headers or {}))
with tracer.start_as_current_span("process_message", context=ctx):
    await handle(msg)
```

See the `otel` skill for the full propagation API and additional propagators (W3C Baggage, etc.).

## Cross-cutting decorator (timed + traced)

```python
from contextlib import contextmanager
import time
import logging
from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

@contextmanager
def timed_operation(name: str, **attrs):
    """Open a span, time the block, log the result."""
    start = time.perf_counter()
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            span.set_attribute(k, v)
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.error(
                "operation_failed",
                extra={"operation": name, "duration_ms": round(elapsed_ms, 2), "error": str(e), **attrs},
            )
            raise
        else:
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.info(
                "operation_completed",
                extra={"operation": name, "duration_ms": round(elapsed_ms, 2), **attrs},
            )


with timed_operation("fetch_user_orders", user_id=user.id):
    orders = await order_repository.get_by_user(user.id)
```

## Summary

1. **OTel for all three signals** — traces, metrics, logs over OTLP.
2. **Set `service.name`, `service.version`, `deployment.environment`** on the Resource.
3. **stdlib `logging` + auto-instrumented LoggingInstrumentor** — don't add `structlog`.
4. **Auto-instrument by default**; add manual spans only for business operations.
5. **Four golden signals** at every external boundary.
6. **Bounded cardinality** on metric attributes; per-request detail goes on spans/logs.
7. **Propagate trace context across queue boundaries** explicitly.
8. **Use semantic-convention attribute names** — see `otel` → `references/attributes.md`.
9. **Sampling configured via env vars** (`OTEL_TRACES_SAMPLER`).

For everything else OTel-related, defer to the `otel` skill.
