# Attributes and semantic conventions

How to pick the right attribute name and put it at the right level. Vendor-neutral.

## Contents

- Core principles
- Attribute placement (resource vs span vs metric)
- Required and recommended attributes
- Common attributes by domain (HTTP, DB, messaging, RPC, errors)
- Registry namespaces (where to look)

## Core principles

1. **Registry first.** Search the [Attribute Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/) before inventing. Use the registered name even if it isn't what you'd choose — it's what every tool, dashboard, and library expects.
2. **Custom attributes must be namespaced.** A bare `order_priority` is invalid. Use reverse-DNS for the project: `com.rask.order.priority`. Custom attributes fragment querying and break tooling — only create them for truly project-specific concepts.
3. **Low cardinality in metric attributes and span names. High cardinality OK in span attributes.** IDs, paths with parameters, free-text — these belong on spans/logs, never on metric labels.
4. **Right level, every time.** Resource attributes describe the producer; span attributes describe a single operation. Never duplicate.
5. **Consistent placement.** Once an attribute lives at a given level, keep it there everywhere. Switching levels across services breaks cross-service queries.

## Attribute placement

| Level                 | What belongs here                             | Examples                                                                                      |
| --------------------- | --------------------------------------------- | --------------------------------------------------------------------------------------------- |
| **Resource**          | Stable identity & environment of the producer | `service.name`, `service.version`, `deployment.environment.name`, `k8s.pod.name`, `host.name` |
| **Scope**             | Identity of the instrumentation library       | `otel.scope.name`, `otel.scope.version`                                                       |
| **Span**              | Request-specific context for one operation    | `http.request.method`, `http.response.status_code`, `db.operation.name`, `order.id`           |
| **Log record**        | Structured log entry context                  | `log.file.path`, body fields, severity                                                        |
| **Metric data point** | Low-cardinality dimensions for aggregation    | `http.request.method`, `http.response.status_code`, `http.route`                              |

### Common placement mistakes

- **`service.name` on every span.** It's a resource attribute. Putting it on spans duplicates data and wastes storage.
- **`k8s.pod.name` on every span.** Kubernetes metadata is resource-level. The Collector's `k8sattributes` processor populates it.
- **`user.id` on the resource.** A resource describes the process, not who's calling it. User identity is per-request → span or log attribute, never resource.
- **High-cardinality metric attributes.** `url.path` or `user.id` on metrics = cardinality explosion. Use `http.route`; per-user details go on spans/logs.
- **Inconsistent placement.** If service A puts `deployment.environment.name` on the resource and service B puts it on every span, cross-service queries break silently.

## Required and recommended resource attributes

### Required

| Attribute      | Purpose                                                                     | How to set              |
| -------------- | --------------------------------------------------------------------------- | ----------------------- |
| `service.name` | Identifies the service. Without it, telemetry falls into `unknown_service`. | env `OTEL_SERVICE_NAME` |

Pick names that are **stable** (don't change between deployments), **unique per logical service**, **human-readable** (`checkout-service`, not `svc-42`), and **case-consistent** (`checkout`, not sometimes `CheckOut`). Pick a convention (kebab-case for this project) and apply across the fleet.

### Highly recommended

| Attribute                     | Purpose                                                                                         | How to set                                                                                                                                       |
| ----------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `service.version`             | Deployment tracking, regression detection                                                       | Derive from build pipeline (git tag, build number); env `OTEL_RESOURCE_ATTRIBUTES=service.version=1.4.2`                                         |
| `service.instance.id`         | Unique per process instance (one per pod). Required for instance-level analysis.                | Generate at startup (UUID v4) or via K8s downward API. Triplet `(service.namespace, service.name, service.instance.id)` must be globally unique. |
| `deployment.environment.name` | Distinguish production from staging/dev. Without it, dashboards mix environments.               | Inject from deployment pipeline, never application code.                                                                                         |
| `service.namespace`           | Groups related services under a product (`acme-webstore` → `checkout`, `payment`, `inventory`). | env `OTEL_RESOURCE_ATTRIBUTES`                                                                                                                   |

In Kubernetes, `k8s.pod.uid` is required for telemetry correlation by the `k8sattributes` processor. Prefer it over `k8s.pod.ip`, which breaks with service meshes (Istio, Linkerd). Set all `k8s.*` via the downward API or the processor — never in application code.

## Common attributes by domain

### HTTP

| Attribute                       | Type        | Notes                                                                                                                 |
| ------------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------- |
| `http.request.method`           | string      | Required. Normalized to `GET`/`POST`/...; unknown values → `_OTHER` with original in `http.request.method_original`.  |
| `http.response.status_code`     | int         | Conditionally required.                                                                                               |
| `url.scheme`                    | string      | Required.                                                                                                             |
| `url.path`                      | string      | **Server spans only.** Replaces part of deprecated `http.target`.                                                     |
| `url.query`                     | string      | Server spans only.                                                                                                    |
| `url.full`                      | string      | **Client spans only.** Replaces deprecated `http.url`.                                                                |
| `http.route`                    | string      | Server spans, conditionally required — the route template, not the raw path. **Use this on metrics, not `url.path`.** |
| `server.address`, `server.port` | string, int | Required. Replaces `net.peer.name` / `net.host.name`.                                                                 |
| `client.address`                | string      | Recommended on server spans.                                                                                          |
| `network.protocol.version`      | string      | `1.1`, `2`, `3`.                                                                                                      |
| `user_agent.original`           | string      | Recommended.                                                                                                          |
| `error.type`                    | string      | Set whenever span status is `ERROR`.                                                                                  |

**Critical:** `url.full` is for `CLIENT` spans only. `url.path`/`url.query` are for `SERVER` spans only. Mixing them is a regression error.

### Database

| Attribute                       | Type        | Notes                                                                |
| ------------------------------- | ----------- | -------------------------------------------------------------------- |
| `db.system.name`                | string      | Required. `postgresql`, `mysql`, `redis`, etc. Replaces `db.system`. |
| `db.operation.name`             | string      | Conditionally required. `SELECT`, `INSERT`, `GET`, etc.              |
| `db.collection.name`            | string      | The table/collection name. Replaces `db.sql.table`.                  |
| `db.namespace`                  | string      | DB/schema name. Replaces `db.name`.                                  |
| `db.query.text`                 | string      | **Opt-in only** (may contain PII). Replaces `db.statement`.          |
| `db.response.status_code`       | string      | Conditionally required.                                              |
| `server.address`, `server.port` | string, int | Required.                                                            |
| `error.type`                    | string      | Conditionally required.                                              |

### Messaging

| Attribute                       | Type   | Notes                                                    |
| ------------------------------- | ------ | -------------------------------------------------------- |
| `messaging.system`              | string | Required. `kafka`, `rabbitmq`, `nats`, etc.              |
| `messaging.operation.name`      | string | Required. `publish`, `receive`, `process`.               |
| `messaging.destination.name`    | string | The topic/queue name.                                    |
| `messaging.message.id`          | string | Recommended.                                             |
| `messaging.consumer.group.name` | string | Conditionally required for systems with consumer groups. |

### RPC

| Attribute                       | Type           | Notes                                 |
| ------------------------------- | -------------- | ------------------------------------- |
| `rpc.system`                    | string         | Required. `grpc`, `connect_rpc`, etc. |
| `rpc.service`, `rpc.method`     | string, string | Recommended.                          |
| `rpc.grpc.status_code`          | int            | Required for gRPC.                    |
| `server.address`, `server.port` | string, int    | Required.                             |

### Errors

| Attribute              | Where          | Notes                                                                                                                |
| ---------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------- |
| `error.type`           | Span attribute | Set whenever span status is `ERROR`. Use a stable identifier — the error class name, an error code, or a typed enum. |
| `exception.type`       | Log record     | The error class name.                                                                                                |
| `exception.message`    | Log record     | Human-readable message.                                                                                              |
| `exception.stacktrace` | Log record     | Full traceback as a single string.                                                                                   |

Stack traces on **log records**, not span events — span events are being deprecated. See `signals.md`.

## Registry namespaces

Before creating a custom attribute, check if it fits an existing namespace. The registry has 80+. Highlights:

### Infrastructure & cloud

`service`, `deployment`, `host`, `os`, `process`, `container`, `k8s`, `cloud`, `faas`, `device`, `aws`, `gcp`, `azure`.

### Protocols & communication

`http`, `rpc`, `graphql`, `dns`, `tls`, `network`, `client`, `server`, `peer`.

### Data systems

`db`, `messaging`.

### URLs & identity

`url`, `user_agent`, `user`, `enduser`, `session`.

### Diagnostics

`error`, `exception`, `code`, `thread`, `log`, `event`.

### Runtimes

`cpython`, `jvm`, `dotnet`, `go`, `nodejs`, `v8js`.

### AI

`gen_ai`, `openai`, `mcp`.

### CI/CD & VCS

`cicd`, `vcs`.

### Other

`feature_flag`, `test`, `geo`, `file`, `artifact`, `hw`, `system`, `telemetry`, `otel`.

Full registry: <https://opentelemetry.io/docs/specs/semconv/registry/attributes/>.

## Examples

```python
# CORRECT — registry attribute for HTTP method
span.set_attribute("http.request.method", "GET")

# WRONG — invents a custom name for a registered concept
span.set_attribute("custom.http.verb", "GET")

# CORRECT — service identity at resource level
# (set via OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES at startup)

# CORRECT — operation-specific data on the span
span.set_attribute("http.request.method", "POST")
span.set_attribute("http.response.status_code", 201)
span.set_attribute("order.id", "abc-123")

# WRONG — resource attribute repeated on every span
span.set_attribute("service.name", "checkout-service")

# CORRECT — metric attributes are bounded
histogram.record(duration_ms, {
    "http.request.method": "GET",
    "http.response.status_code": 200,
    "http.route": "/orders/{id}",
})

# WRONG — unbounded values on metrics
histogram.record(duration_ms, {"user.id": "u-839201", "url.path": "/orders/839201"})
# Fix: put those on the span instead
span.set_attribute("user.id", "u-839201")
span.set_attribute("url.path", "/orders/839201")
```

## The `_OTHER` pattern

Attributes like `http.request.method` accept only a fixed set of values. Unknown values are normalized to `_OTHER`, with the original preserved in `http.request.method_original`. This bounds cardinality while keeping the actual value for debugging.

## References

- [Attribute Registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/) — single source of truth
- [Semantic Conventions Specification](https://opentelemetry.io/docs/specs/semconv/)
- [Resource Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/resource/)
