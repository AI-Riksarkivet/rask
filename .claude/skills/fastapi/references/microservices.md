# Microservices with FastAPI

How a FastAPI service behaves when it's one of many. Opinionated stack:

- **NATS JetStream** — the broker (durable streams, consumer groups, replay).
- **Dapr** — sidecar that exposes Workflow + a `pubsub.jetstream` component that *backs onto NATS*. Apps that already have a Dapr sidecar for Workflow can publish/subscribe via Dapr (`DaprClient.publish_event(...)`) instead of running a second NATS client; messages still land in JetStream. Apps with no need for Workflow can use **`nats-py` directly** and skip the sidecar.
- **Redis** — cache, rate-limit storage, JWT `jti` revocation (direct, not via Dapr state).
- **OpenFGA** — authz. **OTel** — observability.

**No service mesh, no Kafka.** See § Dapr + Kubernetes interplay below for the components.yaml + deployment annotation pattern.

> If you're building a single service, skip this file. Everything here costs operational complexity; only pay for it once a second team owns a second deployable.

## Contents

- When to split (and when not to)
- Service boundary rules
- Inter-service comms — sync (httpx) vs async (NATS JetStream or Dapr pub/sub on JetStream)
- The outbox pattern (atomic write + event)
- Trace context propagation across services
- Service-to-service authn (short-lived JWT from a shared IdP)
- Sharing types — when, and how to keep it from rotting
- API versioning
- **Dapr + Kubernetes interplay** (sidecar wiring, components.yaml, deployment annotations)
- Anti-patterns

## When to split (and when not to)

Split when a **deployment boundary** becomes painful — different release cadence, different on-call team, different scaling profile, different compliance zone. Don't split because "microservices are good architecture." A well-layered monolith with `services/` / `repositories/` (see [`project-template.md`](project-template.md)) gives you 80% of the modularity at 10% of the cost.

| Signal you should split | Signal you should NOT split |
| ----------------------- | --------------------------- |
| Two teams stepping on each other in one repo | "It feels cleaner" |
| Release coupling forces lockstep deploys | One team owns the whole thing |
| One module needs 10× the replicas of the rest | CPU is fine, RAM is fine |
| Different compliance / data-residency for one slice | All data has the same classification |
| One subsystem rewrites well behind a stable contract | Subsystem boundaries still shifting weekly |

## Service boundary rules

Treat these as load-bearing — break them and you've built a distributed monolith (worst of both worlds):

- **One database per service.** No cross-service joins. No `SELECT` against another service's tables. Reads go through that service's API (or via an event projection).
- **Owning service mutates its own data.** Other services subscribe to events to maintain their projections.
- **One bounded context per service.** If service A constantly needs to read service B's data to do its job, they're one context — merge them.
- **No shared mutable libraries.** Shared *types* (request/response schemas) are fine; shared *business logic* couples deploys.

## Inter-service comms — sync vs async

Two patterns, picked per call:

### Sync — `httpx` for "I need the answer to continue"

User-facing latency-critical reads, occasional cross-service queries. One `httpx.AsyncClient` per app, built in lifespan ([`production-patterns.md`](production-patterns.md) § Lifespan):

```python
# services/orders.py
async def get_order_with_user(
    client: httpx.AsyncClient, session: AsyncSession, *, order_id: UUID, token: str,
) -> OrderWithUser:
    order = await session.get(Order, order_id)
    if order is None:
        raise NotFoundError(f"order {order_id}")

    r = await client.get(
        f"http://user-svc/users/{order.user_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=2.0,  # always set a tight timeout for sync calls
    )
    r.raise_for_status()
    return OrderWithUser(order=order, user=User.model_validate(r.json()))
```

**Rules for sync calls:**

- Tight timeout (≤2 s for interactive, ≤500 ms inside a request loop). Never default `httpx`'s no-timeout.
- Retry policy lives in a thin wrapper — don't sprinkle `for attempt in range(3)` across services. See `python-infrastructure` § retry.
- **Never chain more than two services synchronously** (`A → B → C`). Tail latency multiplies; one slow C tanks A. Flip to async events for the second hop.

### Async — NATS JetStream for "I need to tell other services this happened"

Anything that survives the request (notifications, projections, downstream side effects). Producer publishes an event; consumers process it in their own time, with retries and a dead-letter subject. The full NATS JetStream client patterns live in `python-infrastructure` — this file only covers how it lands in a FastAPI handler.

```python
# services/orders.py — emit *after* the DB write commits
async def create_order(
    session: AsyncSession, nats: NatsClient, *, payload: OrderCreate,
) -> Order:
    order = Order(...)
    session.add(order)
    await session.commit()
    await session.refresh(order)

    # Publish AFTER commit — never before. If commit rolls back, no event leaks.
    await nats.publish("orders.created", order.model_dump_json().encode())
    return order
```

The "publish after commit" rule is naive — if the process crashes between `commit()` and `publish()`, the event is lost. That's what the outbox pattern fixes.

## The outbox pattern

When event delivery must be **at-least-once** with respect to DB state, write the event into an `outbox` table in the same transaction as the business write. A separate process (Dapr Workflow activity, NATS connector, or polling worker) reads from `outbox` and publishes:

```python
# services/orders.py
class OutboxEvent(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    subject: str                                # NATS subject
    payload: dict = Field(sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None


async def create_order(session: AsyncSession, *, payload: OrderCreate) -> Order:
    order = Order(...)
    session.add(order)
    session.add(OutboxEvent(
        subject="orders.created",
        payload=order.model_dump(mode="json"),
    ))
    await session.commit()                      # atomic: order + outbox row
    return order
```

A background worker drains the outbox, publishes, marks `published_at`. Idempotency on the consumer side (de-dupe by event `id`) handles the at-least-once.

Pick **outbox** when the event going missing causes data drift across services. Pick **publish-after-commit** when the event is purely informational (a Slack ping). **Dapr Workflow** from `python-infrastructure` § dapr-workflows handles the outbox drain end-to-end as a single durable workflow (one activity reads the outbox row, the next publishes to JetStream, the next marks `published_at` — checkpoint at every step).

## Trace context propagation

OTel auto-instrumentation propagates W3C trace context across `httpx` calls automatically — the receiving service sees the same `trace_id`. For NATS, the SDK won't inject — do it manually with `propagate`:

```python
# producer
from opentelemetry import propagate

headers: dict[str, str] = {}
propagate.inject(headers)
await nats.publish("orders.created", payload, headers=headers)
```

```python
# consumer
ctx = propagate.extract(msg.headers or {})
with tracer.start_as_current_span("orders.created.handler", context=ctx):
    await handle(msg)
```

See `python-infrastructure` § observability for the full pattern. Without context propagation, distributed traces stop at every queue boundary.

## Service-to-service authn

Two patterns, both in [`authn.md`](authn.md) — re-stated here because service-to-service has its own constraints:

- **Service mesh / shared IdP issues a short-lived JWT** (5-15 min) with `sub=service:orders`, `aud=user-svc`. Receiving service verifies via the standard OIDC flow ([`authn.md`](authn.md) § OIDC verification).
- **mTLS is the alternative** — handled at the ingress / sidecar layer, not in app code. If your platform team already runs Istio / Linkerd, lean on that and skip the JWT.

**Never** share a long-lived API key between services — rotation is impossible at scale, and a leaked key affects every caller.

Service identities live in the same OpenFGA store as user identities ([`authz.md`](authz.md)) — a service is just another principal. `can_user_view_order(user, order)` and `can_service_view_order(service, order)` use the same model.

## Sharing types — when and how

Two options for the request/response schemas:

| Option | When | Cost |
| ------ | ---- | ---- |
| **Duplicate** the Pydantic model in both services | Cross-team boundary, slow contract evolution | Manual sync on breaking changes |
| **Shared lib** (`packages/contracts/orders/`) | Same team owns both sides, frequent shape changes | Coupled deploys when the lib bumps major |

Default to **duplicate**. The "DRY" instinct fights you here — a shared types lib creates a hidden release coupling between services that are supposed to deploy independently. If you do build a contracts lib, **only put Pydantic schemas in it** (no logic, no helpers, no service clients).

OpenAPI is your contract. Generate the consumer's types from the producer's `/openapi.json` if you want machine-checked sync without a shared lib.

## API versioning

Version at the **URL prefix**, not header negotiation:

```python
v1 = APIRouter(prefix="/v1/orders", tags=["orders-v1"])
v2 = APIRouter(prefix="/v2/orders", tags=["orders-v2"])
app.include_router(v1)
app.include_router(v2)
```

- Add `v2` alongside `v1`, mark `v1` routes `deprecated=True` ([SKILL.md § OpenAPI flags](../SKILL.md)).
- Keep `v1` alive until consumer traffic drops below a measured threshold (OTel route metrics).
- Breaking event-schema changes follow the same pattern — publish to `orders.created.v2`, keep `orders.created` for the deprecation window.

## Dapr + Kubernetes interplay

Dapr lands as a **sidecar container** injected by the Dapr operator into every pod with the right annotations. The app talks to the sidecar over `127.0.0.1:50001` (gRPC) or `:3500` (HTTP); the sidecar talks to NATS, the workflow state store, etc. From the app's point of view, Dapr is a localhost service.

### Components (cluster-level, applied once per namespace)

```yaml
# k8s/dapr-components/pubsub-jetstream.yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: pubsub                              # the name apps use in DaprClient.publish_event
  namespace: default
spec:
  type: pubsub.jetstream
  version: v1
  metadata:
    - name: natsURL
      value: "nats://nats.messaging.svc.cluster.local:4222"
    - name: name                            # NATS client name; shows up in `nats consumer info`
      value: "dapr"
    - name: durableName
      value: "dapr-consumer"
    - name: queueGroupName                  # load-balance across pods of one app
      value: "{{ .app_id }}"                # templated by Helm/Kustomize per app
    - name: ackWait
      value: "30s"
    - name: maxDeliver
      value: "5"
    - name: backOff
      value: "1s,5s,10s,30s,60s"
```

```yaml
# k8s/dapr-components/workflow-statestore.yaml — Dapr Workflow uses an actor state store for checkpointing
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: actorstatestore
  namespace: default
spec:
  type: state.redis
  version: v1
  metadata:
    - name: redisHost
      value: "redis-master.default.svc.cluster.local:6379"
    - name: actorStateStore                 # MUST be "true" for the workflow runtime
      value: "true"
    - name: redisPassword
      secretKeyRef:
        name: redis-secret
        key: redis-password
auth:
  secretStore: kubernetes
```

`secretKeyRef` + `auth.secretStore: kubernetes` — **never** put a password in `metadata.value`. The pattern is identical to k8s Secret references everywhere.

### Deployment — sidecar injection via annotations

```yaml
# k8s/deployments/checkout.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
spec:
  replicas: 3
  template:
    metadata:
      labels: { app: checkout }
      annotations:
        dapr.io/enabled: "true"
        dapr.io/app-id: "checkout"          # used as default queueGroupName above
        dapr.io/app-port: "8000"            # FastAPI port
        dapr.io/app-protocol: "http"
        dapr.io/log-level: "info"
        dapr.io/metrics-port: "9090"        # sidecar exposes Prometheus metrics here
        dapr.io/sidecar-cpu-request: "100m"
        dapr.io/sidecar-memory-request: "128Mi"
        dapr.io/sidecar-cpu-limit: "500m"
        dapr.io/sidecar-memory-limit: "256Mi"
    spec:
      terminationGracePeriodSeconds: 60     # see kubernetes.md — must accommodate sidecar shutdown too
      containers:
        - name: checkout
          image: registry.example.com/checkout:1.2.3
          ports:
            - { name: http, containerPort: 8000 }
          # ... probes, resources, env, OTel — see kubernetes.md
```

The Dapr operator watches for `dapr.io/enabled: "true"` and mutates the Pod to add the `daprd` container. No app code changes needed.

### What changes vs the no-sidecar deployment

| Concern | No sidecar (nats-py direct) | With Dapr sidecar |
| ------- | --------------------------- | ----------------- |
| `terminationGracePeriodSeconds` | preStop + in-flight + lifespan + buffer | **+ sidecar drain time** (~10 s — Dapr flushes pending acks) |
| Resource requests | App only | App + sidecar (~100m CPU / 128Mi memory baseline) |
| Liveness probe | Hits app's `/livez` | Same — but sidecar has its own at `:3500/v1.0/healthz` |
| Pull policy | One image | Two images (app + `daprio/daprd`); pin `daprd` version cluster-wide |
| First-deploy ordering | App up → ready | App up → sidecar up → app calls sidecar → ready (extra ~2 s) |
| Cost surface | App pod | App pod + ~128Mi RAM per pod for the sidecar |

The added complexity is justified **only** for services that need Dapr Workflow. Pure HTTP / read-cache services should run without the sidecar — annotations omitted.

### Local dev — `dapr init`

```bash
dapr init                                   # installs dapr-placement, dapr-scheduler, redis (for state)
dapr run --app-id checkout \
         --app-port 8000 \
         --resources-path ./k8s/dapr-components \
         -- uv run fastapi dev
```

Same `components/` directory used in k8s. Local NATS via `docker run -d -p 4222:4222 nats:latest -js` or a compose file in `Makefile`.

## Anti-patterns

| Pattern | Why it's wrong | Fix |
| ------- | -------------- | --- |
| Sync chain `A → B → C → D` | Tail latency multiplies; any slow link tanks the front | Flip the second hop to NATS event; let downstream catch up async |
| Cross-service DB joins / shared DB | Couples schema migrations across teams; outage in one DB takes down many services | Each service owns its DB; cross-service reads via API or event projection |
| Two-phase commits / distributed transactions across services | Operationally hellish; locks span network boundaries | Saga pattern via Dapr Workflow (`python-infrastructure` § dapr-workflows), or outbox + idempotent consumers |
| Synchronous HTTP call inside a NATS message handler | The handler is supposed to be retryable; the sync call adds failure modes the retry can't fix | Pre-fetch what you need before the publish; or make the handler enqueue another async job |
| Per-service `BaseSettings` reading 50 env vars from a shared `.env` | Couples deploy config across services | Each service has its own `.env`; share only what genuinely crosses (OTel endpoint, IdP issuer) |
| One huge `shared/` lib with models + clients + helpers | Bumping it forces every service redeploy; sneaky distributed monolith | At most a `contracts/` lib with Pydantic schemas, nothing else |
| Service emitting events with no schema versioning | Consumer breaks silently when producer evolves payload | `subject.vN` discipline (`orders.created.v1`); add new subject for breaking change |
| Building a "framework" wrapping FastAPI for "consistency" across services | Now every service upgrade is blocked on the framework lib | Use the references in this skill; consistency comes from review, not abstraction |
| Adopting a service mesh (Istio, Linkerd) to "solve" microservices | Sidecar per pod with overlapping concerns to what Dapr already covers; mTLS + routing are platform-team mandates, not app concerns | Dapr sidecar handles workflow + pub/sub; if the platform team mandates a mesh, run both — don't roll one yourself |
| Putting Dapr in front of every primitive (state, secrets, configuration, actors) | Sidecar overhead for primitives we already have (Redis-direct for cache/rate-limit, k8s Secrets for secrets, pydantic-settings for config) | Use Dapr **only** for Workflow + `pubsub.jetstream`. Other primitives stay native. |
| `import requests` inside a NATS message handler | Sync blocking call in async path; pool not reused | `httpx.AsyncClient` from lifespan, injected |

## Cross-references

- [`production-patterns.md`](production-patterns.md) — lifespan, `httpx.AsyncClient` setup, shutdown.
- [`authn.md`](authn.md) — OIDC verification, service-to-service JWT.
- [`authz.md`](authz.md) — OpenFGA for both user and service identities.
- [`observability.md`](observability.md) + `otel` skill — trace context propagation across services.
- `python-infrastructure` — NATS JetStream client patterns (`background-jobs.md`), Dapr Workflow (`dapr-workflows.md`), retry/backoff, outbox drain.
