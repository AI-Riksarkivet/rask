# Python SDK

Practical setup and runtime guidance for the OpenTelemetry Python SDK. Vendor-neutral — exporter goes to any OTLP endpoint (local Collector, hosted backend, your own gateway).

## Contents

- Installation
- Environment variables
- Activating the SDK
- Auto-instrumentation
- Custom spans
- Structured logging
- Database query parameters
- Graceful shutdown
- Troubleshooting

## Installation

```bash
uv add opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install
```

`opentelemetry-distro` pulls in the SDK, OTLP exporter, and the `opentelemetry-instrument` launcher. `opentelemetry-bootstrap -a install` scans your installed dependencies and adds the matching instrumentation packages (FastAPI, httpx, asyncpg, redis, requests, SQLAlchemy, Celery, boto3, etc.).

Re-run `opentelemetry-bootstrap -a install` whenever you add a new library that has an instrumentation package.

## Environment variables

| Variable                                           | Default                 | Purpose                                                                                                                                         |
| -------------------------------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `OTEL_SERVICE_NAME`                                | `unknown_service`       | **Required.** Identifies the service.                                                                                                           |
| `OTEL_RESOURCE_ATTRIBUTES`                         | –                       | Comma-separated `key=value` pairs. Set `service.version`, `deployment.environment.name`, optionally `service.namespace`, `service.instance.id`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`                      | `http://localhost:4317` | OTLP endpoint — gRPC on 4317, HTTP/protobuf on 4318.                                                                                            |
| `OTEL_EXPORTER_OTLP_PROTOCOL`                      | `grpc`                  | `grpc`, `http/protobuf`, or `http/json`.                                                                                                        |
| `OTEL_EXPORTER_OTLP_HEADERS`                       | –                       | Auth headers, e.g. `Authorization=Bearer <token>`.                                                                                              |
| `OTEL_TRACES_EXPORTER`                             | `otlp`                  | Defaults to `otlp` in Python (unlike Node.js). Use `console` for local dev.                                                                     |
| `OTEL_METRICS_EXPORTER`                            | `none`                  | **Must be set explicitly to `otlp`** if you want metrics exported.                                                                              |
| `OTEL_LOGS_EXPORTER`                               | `otlp`                  | Defaults to `otlp` in Python.                                                                                                                   |
| `OTEL_TRACES_SAMPLER`                              | `parentbased_always_on` | Leave as default. Sample in the Collector.                                                                                                      |
| `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED` | `false`                 | Set to `true` to wire stdlib `logging` to OTel log records.                                                                                     |
| `OTEL_LOG_LEVEL`                                   | `info`                  | Set to `debug` to see SDK internals.                                                                                                            |

## Activating the SDK

The SDK is activated by running your application under the `opentelemetry-instrument` launcher:

```bash
opentelemetry-instrument python -m rask.api
```

Framework-specific launchers:

```bash
opentelemetry-instrument fastapi dev               # FastAPI dev server
opentelemetry-instrument uvicorn rask.api:app      # uvicorn directly
opentelemetry-instrument python manage.py runserver  # Django
```

Just installing the packages is **not** enough — the SDK is dormant until the launcher runs. If you must set up the SDK programmatically (no launcher), register shutdown hooks manually:

```python
import atexit

atexit.register(tracer_provider.shutdown)
atexit.register(meter_provider.shutdown)
atexit.register(logger_provider.shutdown)
```

## Auto-instrumentation

| Category    | Libraries covered                                                                                      |
| ----------- | ------------------------------------------------------------------------------------------------------ |
| HTTP server | FastAPI, Flask, Django, Starlette, ASGI                                                                |
| HTTP client | httpx, requests, urllib3, aiohttp                                                                      |
| Database    | psycopg, psycopg2, asyncpg, pymongo, redis                                                             |
| ORM         | SQLAlchemy, Tortoise ORM                                                                               |
| Messaging   | Celery, aiokafka, confluent-kafka, pika                                                                |
| AWS         | boto3, botocore                                                                                        |
| gRPC        | grpcio                                                                                                 |
| Logging     | stdlib `logging` (via `LoggingInstrumentor`)                                                           |
| AI/LLM      | [OpenLLMetry](https://github.com/traceloop/openllmetry), [OpenLit](https://github.com/openlit/openlit) |

Full list: <https://opentelemetry.io/ecosystem/registry/?language=python>.

## Custom spans

Add spans for business operations the auto-instrumentation can't see. Get a tracer once per module:

```python
from opentelemetry import trace
from opentelemetry.trace import StatusCode

tracer = trace.get_tracer(__name__)

async def process_order(order: Order) -> Order:
    with tracer.start_as_current_span("order.process") as span:
        span.set_attribute("order.id", order.id)
        span.set_attribute("order.total", order.total)
        try:
            result = await _save_order(order)
        except Exception as e:
            span.set_status(StatusCode.ERROR, f"{type(e).__name__}: {e}")
            raise
        return result
```

### Enriching auto-instrumented spans

When auto-instrumentation creates the span you want to enrich (the `SERVER` span for an inbound HTTP request, the `CLIENT` span for an outbound DB query), grab it from the active context and add attributes:

```python
from opentelemetry import trace

@app.post("/orders")
async def create_order(payload: OrderIn):
    span = trace.get_current_span()
    span.set_attribute("order.id", payload.id)
    span.set_attribute("tenant.id", payload.tenant_id)
    # handler logic
```

`trace.get_current_span()` returns a non-recording no-op span if nothing is active — `set_attribute` is safe to call unconditionally.

### Span status rules in Python

| Outcome                                             | Status            | Notes                                                                    |
| --------------------------------------------------- | ----------------- | ------------------------------------------------------------------------ |
| Operation completed and was explicitly verified     | `OK`              | Don't set speculatively.                                                 |
| No error encountered, but no explicit success check | `UNSET` (default) | Leave it alone.                                                          |
| Final failure (after retries exhausted)             | `ERROR` + message | Include error class: `f"TimeoutError: payment service did not respond"`. |
| Retry attempts that ultimately succeeded            | `UNSET`           | Record each failed attempt as a log record, not as `ERROR`.              |

```python
# BAD: no message
span.set_status(StatusCode.ERROR)

# BAD: traceback in the status message
span.set_status(StatusCode.ERROR, traceback.format_exc())

# GOOD: short, specific message
span.set_status(StatusCode.ERROR, f"TimeoutError: upstream did not respond within 5s")
```

Stack traces belong on a log record with `exception.stacktrace`, not in the span status message. See `signals.md` for the full rules.

## Structured logging

The project standard is stdlib `logging` + `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true`. With that env var set, every `logger.*` call:

- Becomes an OTel log record.
- Picks up the active `trace_id` and `span_id` automatically.
- Forwards through OTLP to the Collector.

```python
import logging

log = logging.getLogger(__name__)

log.info("order.placed", extra={"order_id": order.id, "amount": order.total})
log.warning("rate_limit_approaching", extra={"current_rate": 950, "limit": 1000})
log.error("payment.failed", extra={"order_id": order.id, "provider": "stripe"}, exc_info=True)
```

Pass structured fields via `extra=`. They become log-record attributes; the message string stays low-cardinality and queryable.

### JSON output for stdout collection

If your Collector picks logs up from container stdout (filelog receiver) rather than via OTLP, output single-line JSON so each record stays on one line. Use `python-json-logger`:

```python
import logging
from pythonjsonlogger.json import JsonFormatter

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={"levelname": "level", "asctime": "timestamp"},
))
logging.getLogger().addHandler(handler)
```

`logger.exception()` captures the stack trace into a single `exc_info` field — no multi-line corruption.

**Do not add `structlog`.** Stdlib logging + the OTel handler covers everything.

## Database query parameters

Prepared-statement parameter values are **off by default** because they frequently contain PII and credentials. Read [the cross-language risks](https://opentelemetry.io/docs/specs/semconv/database/sql/) before enabling.

Python has no environment variable for this — enable per instrumentor at SDK startup:

```python
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

Psycopg2Instrumentor().instrument(capture_parameters=True)
```

|                  |                                                                               |
| ---------------- | ----------------------------------------------------------------------------- |
| Default          | `False`                                                                       |
| Attribute key    | `db.statement.parameters` — non-standard, singular attribute with plural name |
| Value shape      | The entire DBAPI parameter tuple stringified via `str(args[1])`               |
| Coverage         | `psycopg`, `psycopg2`, `asyncpg`, `tortoiseorm`, dbapi base                   |
| Silently dropped | `mysql`, `pymysql`, `pymssql`, `sqlite3`, `aiopg`                             |
| No-op            | `sqlalchemy` (has no parameter capture at all)                                |

Treat the Python attribute as **opaque debugging context**, not as a structured replacement for `db.query.parameter.<key>`. Pair it with a redaction processor in the Collector before sending anywhere.

## Context propagation

Auto-instrumentation handles trace context across HTTP boundaries automatically. For anything else — message queues, custom protocols, batched processing — you propagate context manually with `propagate.inject` / `propagate.extract`.

### Across non-HTTP boundaries

```python
from opentelemetry import propagate

# Producer side (e.g. NATS JetStream publish)
carrier: dict[str, str] = {}
propagate.inject(carrier)  # writes traceparent + tracestate + baggage into carrier
await js.publish(subject, payload, headers=carrier)

# Consumer side
ctx = propagate.extract(dict(msg.headers or {}))
with tracer.start_as_current_span("process", context=ctx):
    await handle(msg)
```

`propagate.inject` writes whatever the globally configured propagators emit. The default composite propagator handles W3C Trace Context (`traceparent`, `tracestate`) and W3C Baggage. The carrier is just a `dict[str, str]` — bring your own header dictionary regardless of transport.

### Baggage — cross-cutting business context

Baggage carries small key-value pairs alongside trace context: `tenant.id`, `request.id`, feature flags, debug flags. It auto-propagates through every outgoing call once it's in the active context.

```python
from opentelemetry import baggage, context

# Set at the request boundary (FastAPI middleware, NATS consumer, etc.)
ctx = baggage.set_baggage("tenant.id", "acme")
ctx = baggage.set_baggage("request.id", req_id, context=ctx)
token = context.attach(ctx)
try:
    # Every span and every outgoing call inside this block carries the baggage
    await handle_request()
finally:
    context.detach(token)

# Read in any downstream code
tenant = baggage.get_baggage("tenant.id")
```

**Never put secrets, PII, tokens, or credentials in baggage.** Baggage travels in HTTP headers, gets logged, gets cached. IDs and references only — resolve the actual data server-side. Keep total baggage under ~4 KB; many proxies cap header sizes around 8 KB.

Baggage is not auto-copied to span attributes. If you want `tenant.id` searchable in your trace backend, also call `span.set_attribute("tenant.id", tenant)` at the relevant span.

### Span Links — consumer-to-producer relationships

When a span isn't a child of its causal predecessor — typically batch consumers processing many producer messages in one span — use a `Link` instead of a parent-child relation.

```python
from opentelemetry.trace import Link

upstream_contexts = [propagate.extract(dict(m.headers or {})) for m in batch]
links = [
    Link(trace.get_current_span(c).get_span_context())
    for c in upstream_contexts
    if trace.get_current_span(c).get_span_context().is_valid
]

with tracer.start_as_current_span("batch.process", links=links) as span:
    span.set_attribute("batch.size", len(batch))
    for msg in batch:
        await handle(msg)
```

Backends render links as sidebar references on the span — you can navigate from the batch span to any of the producer traces.

## Graceful shutdown

`opentelemetry-instrument` registers an `atexit` hook automatically. On normal exit (including unhandled exceptions in most WSGI/ASGI servers), pending spans/metrics/logs are flushed before the process terminates.

Abrupt termination (`SIGKILL`, OOM kill, segfault) bypasses all hooks — no in-process mitigation exists. Run the Collector co-located (DaemonSet sidecar, host agent) so the network hop doesn't add to your shutdown deadline.

For programmatic setups without the launcher, call `provider.shutdown()` on every provider before exit. Shutdown blocks up to 30 s (default) waiting for export to complete.

## Troubleshooting

### No telemetry appearing

1. **Confirm the launcher is running:** `ps aux | grep opentelemetry-instrument`. If you're running plain `python`, the SDK is dormant.
2. **Check exporter env vars:** `echo $OTEL_TRACES_EXPORTER` should be `otlp` (or `console`), not `none`. Same for `OTEL_METRICS_EXPORTER` (`none` is the default — must be set to `otlp` explicitly).
3. **List installed instrumentations:** `opentelemetry-bootstrap -a requirements` — shows which packages would be added based on your dependencies. Missing? Run `opentelemetry-bootstrap -a install` in the same venv.

### Connection errors

```
Failed to export batch. UNAVAILABLE: failed to connect to all addresses
```

The SDK is fine; the Collector isn't reachable.

- No Collector running? Start one (`references/collector.md`) or temporarily set `OTEL_TRACES_EXPORTER=console`.
- Wrong endpoint? Check `OTEL_EXPORTER_OTLP_ENDPOINT`.
- Wrong port? gRPC uses 4317; HTTP/protobuf uses 4318.

### Debug logging

```bash
export OTEL_LOG_LEVEL=debug
opentelemetry-instrument python -m rask.api
```

Reveals exporter activity, span creation, and configuration issues.
