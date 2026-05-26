# Microservices with FastAPI

How a FastAPI service behaves when it's one of many. This file is opinionated about *our* stack — pure FastAPI + the `python-infrastructure` primitives (NATS JetStream, DBOS, Redis, OpenFGA, OTel). **No Dapr, no service mesh, no Kafka.**

> If you're building a single service, skip this file. Everything here costs operational complexity; only pay for it once a second team owns a second deployable.

## Contents

- When to split (and when not to)
- Service boundary rules
- Inter-service comms — sync (httpx) vs async (NATS JetStream)
- The outbox pattern (atomic write + event)
- Trace context propagation across services
- Service-to-service authn (short-lived JWT from a shared IdP)
- Sharing types — when, and how to keep it from rotting
- API versioning
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

When event delivery must be **at-least-once** with respect to DB state, write the event into an `outbox` table in the same transaction as the business write. A separate process (DBOS workflow, NATS connector, or polling worker) reads from `outbox` and publishes:

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

Pick **outbox** when the event going missing causes data drift across services. Pick **publish-after-commit** when the event is purely informational (a Slack ping). DBOS workflows from `python-infrastructure` handle the outbox drain end-to-end.

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

## Anti-patterns

| Pattern | Why it's wrong | Fix |
| ------- | -------------- | --- |
| Sync chain `A → B → C → D` | Tail latency multiplies; any slow link tanks the front | Flip the second hop to NATS event; let downstream catch up async |
| Cross-service DB joins / shared DB | Couples schema migrations across teams; outage in one DB takes down many services | Each service owns its DB; cross-service reads via API or event projection |
| Two-phase commits / distributed transactions across services | Operationally hellish; locks span network boundaries | Saga pattern via DBOS workflows (`python-infrastructure`), or outbox + idempotent consumers |
| Synchronous HTTP call inside a NATS message handler | The handler is supposed to be retryable; the sync call adds failure modes the retry can't fix | Pre-fetch what you need before the publish; or make the handler enqueue another async job |
| Per-service `BaseSettings` reading 50 env vars from a shared `.env` | Couples deploy config across services | Each service has its own `.env`; share only what genuinely crosses (OTel endpoint, IdP issuer) |
| One huge `shared/` lib with models + clients + helpers | Bumping it forces every service redeploy; sneaky distributed monolith | At most a `contracts/` lib with Pydantic schemas, nothing else |
| Service emitting events with no schema versioning | Consumer breaks silently when producer evolves payload | `subject.vN` discipline (`orders.created.v1`); add new subject for breaking change |
| Building a "framework" wrapping FastAPI for "consistency" across services | Now every service upgrade is blocked on the framework lib | Use the references in this skill; consistency comes from review, not abstraction |
| Adopting Dapr / service-mesh sidecars to "solve" microservices | Adds a sidecar per pod, duplicates every primitive we already have | Pure FastAPI + `python-infrastructure` primitives. Sidecars only when the platform team mandates them |
| `import requests` inside a NATS message handler | Sync blocking call in async path; pool not reused | `httpx.AsyncClient` from lifespan, injected |

## Cross-references

- [`production-patterns.md`](production-patterns.md) — lifespan, `httpx.AsyncClient` setup, shutdown.
- [`authn.md`](authn.md) — OIDC verification, service-to-service JWT.
- [`authz.md`](authz.md) — OpenFGA for both user and service identities.
- [`observability.md`](observability.md) + `otel` skill — trace context propagation across services.
- `python-infrastructure` — NATS JetStream client patterns, DBOS workflows, retry/backoff, outbox drain.
