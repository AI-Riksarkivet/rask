# FastAPI Observability

What's specific to FastAPI when instrumenting with OpenTelemetry. Everything else (SDK setup, exporters, sampling, semantic conventions, log forwarding, NATS context propagation) lives in the **`otel`** and **`python-infrastructure`** skills — don't duplicate.

> If you only read one line: run `opentelemetry-instrument python -m your_app` after `opentelemetry-bootstrap -a install`. That auto-wires FastAPI, httpx, asyncpg, redis, SQLAlchemy and most of our stack. The rest of this file is for the bits auto-instrumentation can't cover.

## Contents

- When auto-instrumentation is enough
- When to call `FastAPIInstrumentor.instrument_app` manually
- Exclude noisy endpoints (`/livez`, `/readyz`, `/metrics`)
- Request hooks — custom attributes from headers
- Manual spans inside routes — only for business operations
- What NOT to do
- Quick checklist for a new FastAPI service

## When auto-instrumentation is enough

Almost always. The `opentelemetry-instrumentation-fastapi` package (installed by `opentelemetry-bootstrap`) hooks the ASGI lifecycle and emits one span per request, named `{METHOD} {http.route}` (so `GET /items/{item_id}` — the **template**, not the resolved path; that's what you want for cardinality).

You get for free:

- One span per HTTP request with `http.method`, `http.route`, `http.status_code`, `network.peer.address`, `user_agent.original`, request/response sizes.
- Async context propagation across `await` boundaries.
- Child spans from instrumented libraries (httpx outgoing calls, asyncpg queries, redis calls) automatically parented under the request span.

Add manual spans only for domain operations the framework can't see (`process_order`, `validate_invoice`).

## When to call `FastAPIInstrumentor.instrument_app` manually

Use the programmatic form **only** when one of these is true:

- You can't run `opentelemetry-instrument` as the entrypoint (some PaaS sandboxes, Lambda).
- You need to pass `excluded_urls` or `*_hook` arguments that the env-var-driven path doesn't expose.
- You're building a library that ships FastAPI under the hood.

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# After app = FastAPI(...) but before any requests are served:
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="/livez,/readyz,/metrics",
)
```

## Exclude noisy endpoints

Health and metrics endpoints get hit every few seconds by infrastructure. Tracing them buries the signal.

```python
FastAPIInstrumentor.instrument_app(app, excluded_urls="/livez,/readyz,/metrics")
```

Or set the env var globally (applies to all auto-instrumented frameworks):

```bash
export OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="/livez,/readyz,/metrics"
```

## Request hooks (custom attributes from headers)

When the request carries useful attribution you want on every span — tenant id, request id, version header — use a `server_request_hook`. It runs once per request, before the route handler.

```python
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


def server_request_hook(span: trace.Span, scope: dict) -> None:
    if not (span and span.is_recording()):
        return
    headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
    if rid := headers.get("x-request-id"):
        span.set_attribute("request.id", rid)
    if tenant := headers.get("x-tenant-id"):
        span.set_attribute("rask.tenant.id", tenant)


FastAPIInstrumentor.instrument_app(app, server_request_hook=server_request_hook)
```

**Don't** put PII (email, raw user id, auth tokens) in span attributes — those go to the trace backend forever. Hash or omit.

If the hook needs to feed downstream code (logs, repository methods), set a `ContextVar` in the hook and read it elsewhere — see `production-patterns.md` § Request-scoped data via `ContextVar`.

## Manual spans inside routes — only for business operations

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


@router.post("/orders")
async def create_order(payload: OrderCreate, svc: OrderServiceDep) -> OrderPublic:
    # The HTTP span is created by FastAPIInstrumentor — don't wrap the whole handler.
    # Wrap the *business operation* so its name shows up in the trace.
    with tracer.start_as_current_span("order.create") as span:
        span.set_attribute("rask.order.priority", payload.priority)
        order = await svc.create(payload)
    span.set_attribute("rask.order.id", str(order.id))
    return order
```

Span names should describe **what the code is doing**, not where it lives (`order.create`, not `OrderService.create`). See the `otel` skill `references/signals.md` for the full naming rules.

## What NOT to do

| Anti-pattern                                                 | Why                                                                             | Do this instead                                                                  |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `span.record_exception(e)` in route / handler                | Deprecated in OTel; events are second-class. See `otel` SKILL.md § Top gotchas. | Log the exception inside the active span context — `trace_id`/`span_id` ride along. |
| Wrapping the whole route body in `tracer.start_as_current_span` | Duplicates the auto-created HTTP span — two layers of noise.                  | Wrap only the *business operation* inside the route.                              |
| Custom `BackgroundTasks` traced as a child of the request    | Tasks run *after* the response — the request span is already closed.            | Don't use `BackgroundTasks` for anything you want to observe. Use NATS JetStream + propagate context (see `python-infrastructure`). |
| Setting `service.name` on every span                         | Resource attribute, not span attribute. Wasteful and wrong.                     | Set via env: `OTEL_SERVICE_NAME=rask-api`.                                       |
| `OTLPSpanExporter(endpoint="http://collector:4317")` in code | Hard-coded endpoints don't survive multi-env deploys.                           | `OTEL_EXPORTER_OTLP_ENDPOINT` env var.                                           |
| SDK-side sampling (`TraceIdRatioBased(0.1)`)                 | Drops traces before you know if the request errored.                            | Always-on at the SDK; tail sample in the Collector. See `otel` `references/collector.md`. |

## Quick checklist for a new FastAPI service

1. `uv add opentelemetry-distro opentelemetry-exporter-otlp` + `opentelemetry-bootstrap -a install`.
2. Set `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_METRICS_EXPORTER=otlp`.
3. Launch with `opentelemetry-instrument python -m your_app`.
4. Add `excluded_urls="/livez,/readyz,/metrics"` (either via env or `instrument_app`).
5. Add a `server_request_hook` only if you have a stable, low-cardinality attribute worth attaching to every span.
6. Wrap **business operations** (not handlers) in manual spans where the framework can't infer them.

Stop there. Sampling, redaction, multi-tenant filtering — all of that belongs in the Collector pipeline (`otel` `references/collector.md`).
