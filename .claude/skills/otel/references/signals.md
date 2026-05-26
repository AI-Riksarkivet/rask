# Signals: spans, metrics, logs

How to author each of the three signals correctly. Vendor-neutral. Examples are Python but the rules apply to any language.

## Contents

- Symptom-to-cause workflow
- Spans — naming, kind, status, hygiene
- Metrics — instrument types, units, cardinality
- Logs — structured, severity, trace correlation
- Sampling discipline

## Symptom-to-cause workflow

> Metrics surface problems → Traces pinpoint location → Logs explain causation.

| Signal      | Use for                                                                    |
| ----------- | -------------------------------------------------------------------------- |
| **Metrics** | Alerting, SLOs, dashboards, trends, capacity.                              |
| **Traces**  | Request flow, latency breakdown, dependency map, error locality.           |
| **Logs**    | Audit trails, event detail, why something failed once you've localized it. |

Every signal carries the active `trace_id` / `span_id` so you can navigate from an alert → trace → log line.

## Spans

### Naming — low cardinality, `{verb} {object}`

Span names must be bounded. The number of unique names in a system is small and finite.

| Anti-pattern                       | Correct               | Fix                                 |
| ---------------------------------- | --------------------- | ----------------------------------- |
| `GET /api/users/12345`             | `GET /api/users/{id}` | Use route template, not raw path    |
| `SELECT * FROM orders WHERE id=99` | `SELECT orders`       | Use table name, not the query       |
| `process_payment_for_user_jane`    | `process payment`     | User identity is an attribute       |
| `validation_failed`                | `validate user_input` | Name the operation, not the outcome |

Per-signal patterns:

| Signal      | Format                                     | Example               |
| ----------- | ------------------------------------------ | --------------------- |
| HTTP server | `{method} {http.route}`                    | `GET /users/{id}`     |
| HTTP client | `{method} {url.template}` or `{method}`    | `POST /checkout`      |
| Database    | `{db.operation.name} {db.collection.name}` | `SELECT orders`       |
| RPC         | `{rpc.service}/{rpc.method}`               | `UserService/GetUser` |
| Messaging   | `{operation} {destination}`                | `publish shop.orders` |

Path parameterization: replace IDs and UUIDs with placeholders before they reach a span name or attribute. Most frameworks expose `request.route` or equivalent — use that. Fall back to regex only if no framework value is available.

### Span kind — match the communication pattern

| Kind       | Use when                                | Examples                          |
| ---------- | --------------------------------------- | --------------------------------- |
| `SERVER`   | Handling an inbound synchronous request | Incoming HTTP, incoming gRPC      |
| `CLIENT`   | Making an outbound synchronous request  | HTTP call, DB query, outbound RPC |
| `PRODUCER` | Initiating an async operation           | Publishing to a queue/topic       |
| `CONSUMER` | Processing an async operation           | Receiving from a queue            |
| `INTERNAL` | No remote counterpart                   | In-memory computation             |

Common mistakes:

- `INTERNAL` for everything — DB calls are `CLIENT`, HTTP handlers are `SERVER`.
- `CLIENT` for queue publishes — publishing is async, use `PRODUCER`.
- `SERVER` for queue consumers — the publisher isn't waiting, use `CONSUMER`.

### Status code

| Span kind | HTTP 1xx-3xx | 4xx                          | 5xx         | No response |
| --------- | ------------ | ---------------------------- | ----------- | ----------- |
| `CLIENT`  | `UNSET`      | **`ERROR`**                  | **`ERROR`** | **`ERROR`** |
| `SERVER`  | `UNSET`      | `UNSET` (server did its job) | **`ERROR`** | **`ERROR`** |

A 400 on a server span is **not** an error — the server correctly rejected a bad request. The same 400 on the matching client span **is** an error — the client's request failed.

General rules:

- Default is `UNSET`. Only set `OK` when application logic explicitly verifies success (validated response, completed transaction).
- Set `ERROR` only when the failure is final. Retried-then-succeeded operations stay `UNSET`.
- Every `ERROR` status carries a message with the error class: `f"TimeoutError: payment service did not respond within 5s"`. Not `"something went wrong"`. Not the stack trace — that goes on a log record.

### Exceptions: log records, not span events

The Span Event API is being [deprecated](https://github.com/open-telemetry/opentelemetry-specification/blob/main/oteps/4430-span-event-api-deprecation-plan.md). Don't call `span.record_exception(e)` in new code. Emit a log record inside the active span context — it carries `trace_id` and `span_id` automatically:

```python
import logging, traceback

log = logging.getLogger(__name__)

try:
    charge_payment(order)
except Exception as e:
    span.set_status(StatusCode.ERROR, f"{type(e).__name__}: {e}")
    log.error("order.charge.failed", extra={
        "order.id": order.id,
        "exception.type": type(e).__name__,
        "exception.message": str(e),
        "exception.stacktrace": traceback.format_exc(),
    })
    raise
```

### Span hygiene

- **Root spans must not be `CLIENT` or `PRODUCER`.** A `CLIENT` root means an outbound call is the first thing the trace saw — usually broken context propagation or a headless task (cron, worker) missing a wrapping `SERVER`/`INTERNAL` span. Wrap headless operations explicitly.
- **Every `CLIENT`/`PRODUCER` span needs a parent.** Auto-instrumentation handles request-scoped calls; background tasks need a manual root span.
- **No orphan spans.** A span with `parent_span_id` but no matching parent in the trace = broken propagation or the parent was sampled out.
- **Cap `INTERNAL` spans at ~10 per trace per service.** More signals over-instrumentation. Replace per-item spans with a single batch span carrying `batch.size`.
- **Avoid >20 spans under 5 ms per trace.** Tight loops inflate storage without value.

### Span attributes

Auto-instrumentation sets protocol-level attributes (`http.request.method`, `db.operation.name`). Add domain attributes that answer: _"when investigating this span during an incident, what context would I need?"_

| Domain        | Examples                                        |
| ------------- | ----------------------------------------------- |
| Commerce      | `order.id`, `cart.item_count`, `payment.method` |
| Auth          | `user.id`, `user.role`, `auth.method`           |
| Multi-tenant  | `tenant.id`, `tenant.plan`                      |
| Feature flags | `feature_flag.key`, `feature_flag.variant`      |

Naming: dot-separated namespaces (`order.id`, not `orderId`). See `attributes.md` for the registry.

### Sampling

**Use the default `AlwaysOn` sampler in the SDK. Do not change it.**

SDK-side sampling makes an irreversible decision at the head of the trace, before the outcome is known. A trace that looked unremarkable may contain an error or a latency spike — all lost if the SDK dropped it.

Defer all sampling to the Collector:

```
SDK (AlwaysOn) → Collector (head or tail sampling) → Backend (retention)
```

If you sample in the Collector, materialize RED metrics from spans **before** the sampling step (via the `spanmetricsconnector`) — derived metrics from sampled traces are inaccurate.

## Metrics

### Instrument types

| Type              | Use for                         | Example                             |
| ----------------- | ------------------------------- | ----------------------------------- |
| **Counter**       | Monotonic totals (only goes up) | Requests served, errors, bytes sent |
| **UpDownCounter** | Totals that go both ways        | Active connections, queue depth     |
| **Histogram**     | Distributions (p50/p95/p99)     | Request latency, response size      |
| **Gauge**         | Point-in-time snapshots         | CPU %, memory, temperature          |

Decision tree:

- Need percentiles? **Histogram**.
- Counting things that never decrease? **Counter**.
- Tracking a value that goes up and down? **UpDownCounter**.
- Sampling a value that exists independently? **Gauge** (usually observable/async).

Sync vs async (observable):

- **Sync** — updated inline (`counter.add(1)` inside a handler).
- **Async** — register a callback the SDK invokes at collection time (poll connection pool depth, CPU usage).

### Check auto-instrumentation first

Many semconv metrics are already emitted by auto-instrumentation packages. Duplicating them wastes money and creates conflicting series.

| Domain      | Library                                                      | Metric emitted                                                |
| ----------- | ------------------------------------------------------------ | ------------------------------------------------------------- |
| HTTP server | `opentelemetry-instrumentation-fastapi`, `-flask`, `-django` | `http.server.request.duration`, `http.server.active_requests` |
| HTTP client | `opentelemetry-instrumentation-httpx`, `-requests`           | `http.client.request.duration`                                |
| DB          | `opentelemetry-instrumentation-psycopg`, `-asyncpg`          | `db.client.operation.duration`                                |
| Messaging   | `opentelemetry-instrumentation-kafka`, `-celery`             | `messaging.process.duration`, `messaging.publish.duration`    |

Before creating a custom metric:

1. List installed instrumentation packages (`opentelemetry-bootstrap -a requirements`).
2. Look up which metrics each emits.
3. Only create a custom one if no existing covers your use case.

### Naming rules

- **Semconv first.** Search the [metrics semconv](https://opentelemetry.io/docs/specs/semconv/general/metrics/) before inventing.
- **Never put the unit in the name.** `orders.value` with unit `{USD}` — not `orders.value.usd`. The unit is a separate field.
- **No name collisions with attributes.** Don't name a metric `http.response.status_code` — that's an attribute.

### Units

Use [UCUM](https://ucum.org/) notation. A metric without a unit is uninterpretable.

| Unit         | Meaning                                   |
| ------------ | ----------------------------------------- |
| `s`          | Seconds                                   |
| `ms`         | Milliseconds                              |
| `By`         | Bytes                                     |
| `1`          | Dimensionless                             |
| `{requests}` | Annotation — counts of something specific |

Consistency: all emitters of the same metric name must use the same unit and (for histograms) the same bucket boundaries.

### Cardinality — the #1 cost driver

Cardinality = the number of unique time series. Each additional attribute multiplies your series count by its value-count:

```
method:    5
route:     50
status:    5
instances: 10
→ 5 × 50 × 5 × 10 = 12,500 series
```

| Series         | Zone       | Action                            |
| -------------- | ---------- | --------------------------------- |
| <1,000         | Minimal    | Room to add dimensions            |
| 1,000–10,000   | Ideal      | Good balance                      |
| 10,000–50,000  | Acceptable | Monitor monthly                   |
| 50,000–100,000 | Caution    | Review attributes                 |
| >100,000       | Danger     | Remove unbounded attributes       |
| >1,000,000     | Critical   | Backend instability, massive cost |

**Never on metrics:** `user.id`, `request.id`, `order.id`, `url.full`, `timestamp`, `ip.address`, raw `url.path` (likely contains IDs).

Normalize before recording:

```python
# Path: /users/123 → /users/{id} — or use http.route from the framework
import re
def normalize(path: str) -> str:
    return re.sub(r"/\d+", "/{id}", path)
```

Per-request identifiers belong on **spans** and **logs**, where they're indexed but not multiplied.

## Logs

### Structured key-value pairs, never interpolation

```python
# BAD: unstructured
log.info(f"User {user_id} placed order {order_id}")

# GOOD: structured
log.info("order.placed", extra={"user.id": user_id, "order.id": order_id, "amount": amount})
```

Don't spread entire request objects — explicitly pick safe fields. A request body might contain passwords, tokens, or PII.

### Severity

Always set a numeric severity. Records left at `UNSET` lose filtering and alerting capability.

| Level          | Use for                       | Examples                          |
| -------------- | ----------------------------- | --------------------------------- |
| `DEBUG` (5)    | Development diagnostics       | Variable values                   |
| `INFO` (9)     | Request lifecycle, operations | Request start/end, job completion |
| `WARNING` (13) | Recoverable anomalies         | Retry attempts, fallback used     |
| `ERROR` (17)   | Failures needing attention    | Exceptions, unavailable service   |

Never log expected behavior at `ERROR`. A user typing the wrong password is `INFO`, not `ERROR`. Use `9` (`INFO`) for access/audit logs.

### Trace correlation

Every log emitted inside an active span carries `trace_id` and `span_id`. With `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true` this is automatic via the `LoggingInstrumentor`. No helper needed.

Without auto-instrumentation, extract manually:

```python
from opentelemetry import trace

def _ctx() -> dict[str, str]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }

log.info("order.placed", extra={**_ctx(), "order.id": order_id})
```

### Log events vs regular logs

A log record with a non-empty `event_name` (or `otel.event.name` attribute) is an **event** — a named occurrence with a stable schema that tooling can categorize.

Use log events for occurrences that meet **both**:

- **Stable schema** — same attribute set every time.
- **Business or operational milestone** — something you'd count, alert on, or filter by (deployments, signups, payment completions).

Everything else (diagnostic logging, variable-shape data) stays a regular log record.

### Exception stack traces — single line

A multi-line stack trace breaks the one-record-per-line contract that container log collectors rely on. Serialize the trace as a single string field:

```python
# logger.exception() automatically captures the trace; python-json-logger serializes it as exc_info
log.exception("order.failed", extra={"order.id": order_id})
```

Output:

```json
{
	"level": "error",
	"message": "order.failed",
	"order.id": "abc-123",
	"exc_info": "TypeError: ...\n  at ..."
}
```

### Stdout vs OTLP for log delivery

| Method                          | Pros                                                                       | Cons                                                                                     |
| ------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Stdout only + filelog collector | All logs visible via `kubectl logs`; library/bootstrap/crash logs included | Needs a collector to forward                                                             |
| OTLP only (Logs SDK)            | Native OTLP, no parsing                                                    | Bypasses runtime — `kubectl logs` empty; bootstrap/crash logs lost; outage = silent loss |
| Both                            | Belt-and-suspenders                                                        | **Duplicate records** at the backend — doubles cost                                      |

**Default: stdout/stderr only.** Write structured JSON; let the Collector's filelog receiver pick them up. Do not run both the OTel Logs SDK exporter and a filelog receiver without explicit deduplication.

## Cross-references

- Attribute names — see `attributes.md`.
- Python SDK specifics — see `python-sdk.md`.
- Collector pipelines (where signals land) — see `collector.md`.
- Project conventions (golden signals, propagation across NATS) — see `python-infrastructure/References/observability.md`.
