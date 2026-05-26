# Redis (the design-choice view)

Adding Redis is a deliberate choice — not every FastAPI service needs it. This file is the **decision matrix + wiring hub**: when Redis is the right tool vs when NATS/Postgres/in-process is, how the single shared client is built, and one explicit example of each thing we actually use Redis for. Pattern deep-dives live in [`cache.md`](cache.md) and [`rate-limiting.md`](rate-limiting.md); framework-agnostic patterns (stampede protection, dedup windows) live in `python-infrastructure` § caching. Redis-operator concerns (eviction policy, persistence config, cluster sizing) are not in any skill — defer to the upstream Redis docs.

## Contents

- When to add Redis (and when not to)
- One shared client — built in lifespan, reused everywhere
- Use 1 — Cache (`cache_aside`)
- Use 2 — Rate limiting (`slowapi` storage backend)
- Use 3 — JWT `jti` revocation list
- Use 4 — Single-process SSE fan-out (Pub/Sub)
- Use 5 — Distributed lock for "at most one worker"
- What we do NOT use Redis for
- Anti-patterns

## When to add Redis (and when not to)

Default to **no Redis**. Add it only when one of these is true and the existing primitives can't cover the case:

| You need… | Right tool | Why |
| --------- | ---------- | --- |
| Sub-ms read-through cache for hot DB rows | **Redis** | Postgres can't do single-digit ms at 10k QPS |
| Per-IP / per-user rate limit shared across pods | **Redis** | `slowapi` in-memory backend = per-pod limits = wrong number |
| Stateless-JWT revocation ("log out everywhere", leaked-token rotation) | **Redis** | TTL'd `jti` set; small footprint; perfect fit |
| Cross-pod broadcast to WebSocket clients | **NATS JetStream**, *not Redis* | One broker (we already run NATS); persistence, replay, consumer groups |
| Reliable background jobs / sagas | **Dapr Workflow**, *not Redis* | Activity-level checkpointing; survives pod crashes. See `python-infrastructure` § dapr-workflows. |
| Session storage | **Stateless JWT**, *not Redis* | No revocation needed; no shared mutable state |
| Cache full HTTP responses | **CDN / reverse proxy**, *not in-app middleware* | Auth-aware caching is a foot-gun (see [`cache.md`](cache.md)) |
| Pub/Sub across services | **NATS**, *not Redis pub/sub* | We don't run two brokers |

**Rule of thumb:** if NATS or Postgres can do it, use those. Redis enters when you specifically need fast TTL'd key-value with atomic ops.

## One shared client — built in lifespan, reused everywhere

The same `app.state.redis` powers every Redis use case below. Build it once.

```python
# core/redis.py
import redis.asyncio as redis

from app.core.config import settings


def make_redis() -> redis.Redis:
    return redis.Redis.from_pool(redis.ConnectionPool.from_url(
        str(settings.REDIS_URL),
        max_connections=50,
        decode_responses=True,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        retry_on_timeout=True,
        health_check_interval=30,
    ))
```

```python
# main.py — lifespan
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.core.redis import make_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... db_engine, http ...
    app.state.redis = make_redis()
    await app.state.redis.ping()           # fail-fast on bad URL / unreachable host
    yield
    await app.state.redis.aclose()


app = FastAPI(lifespan=lifespan)
```

```python
# api/deps.py
from typing import Annotated
from fastapi import Depends, Request
import redis.asyncio as redis


def get_redis(request: Request) -> redis.Redis:
    return request.app.state.redis


RedisDep = Annotated[redis.Redis, Depends(get_redis)]
```

Routes consume `RedisDep`. Never read `request.app.state.redis` directly — testing breaks.

Override in tests via `app.dependency_overrides[get_redis] = lambda: fake_redis` ([`dependencies.md`](dependencies.md) § Overriding dependencies in tests).

## Use 1 — Cache (`cache_aside`)

The most common reason to add Redis. Full pattern lives in [`cache.md`](cache.md); one-line summary: use the `cache_aside` helper **inside service methods** (not at the route boundary), invalidate **after** the DB commit.

```python
# services/users.py
async def get_user(session: AsyncSession, r: redis.Redis, *, user_id: UUID) -> User | None:
    return await cache_aside(
        r, key=f"user:{user_id}", ttl_s=300, model=User,
        fetch=lambda: session.get(User, user_id),
    )
```

For the helper, mutation→invalidation rule, lifespan cache-warming, and the "why NOT a response-caching middleware" anti-pattern, see [`cache.md`](cache.md).

## Use 2 — Rate limiting (`slowapi` storage backend)

The second-most-common reason. Same `app.state.redis` becomes the `slowapi` storage URI so per-IP / per-user limits are shared across pods. Full setup in [`rate-limiting.md`](rate-limiting.md); the only thing worth restating here is **the wiring is one line**:

```python
limiter.storage_uri = str(settings.REDIS_URL).replace("redis://", "async+redis://")
```

Without Redis, `slowapi` falls back to in-process storage — fine for a single pod, wrong the moment you scale to 2.

## Use 3 — JWT `jti` revocation list

Stateless JWT means "no server-side state to revoke." When you genuinely need revocation (explicit logout, compliance "log out everywhere", leaked-token rotation), add a Redis-backed **revocation list** keyed by the JWT's `jti` claim. Each entry's TTL matches the token's remaining lifetime — Redis drops it automatically once the token would expire anyway.

```python
# core/jwt_revocation.py
import redis.asyncio as redis

REVOKED_PREFIX = "jwt:revoked:"


async def revoke(r: redis.Redis, *, jti: str, ttl_s: int) -> None:
    """Mark a token as revoked. ttl_s = seconds until the token would expire anyway."""
    await r.set(f"{REVOKED_PREFIX}{jti}", "1", ex=ttl_s)


async def is_revoked(r: redis.Redis, *, jti: str) -> bool:
    return await r.exists(f"{REVOKED_PREFIX}{jti}") > 0
```

```python
# api/routes/auth.py
from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUserDep, RedisDep
from app.core.jwt_revocation import revoke

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/logout")
async def logout(user: CurrentUserDep, r: RedisDep) -> Message:
    # `user.token_claims` set by the authn dep; contains `jti` and `exp`.
    jti = user.token_claims["jti"]
    exp = user.token_claims["exp"]
    ttl_s = max(0, exp - int(datetime.now(UTC).timestamp()))
    await revoke(r, jti=jti, ttl_s=ttl_s)
    return Message(message="Logged out")
```

Then in the authn dep, after signature + expiry checks succeed:

```python
# api/authn_deps.py — excerpt from authn.md flow
async def get_current_user(token: TokenDep, r: RedisDep) -> User:
    claims = verify_jwt(token)              # signature + exp check
    if await is_revoked(r, jti=claims["jti"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
    return await load_user(claims["sub"])
```

**Issue tokens with a `jti` claim** when you sign them (`uuid4().hex` is fine). Without `jti` there's nothing to revoke. See [`authn.md`](authn.md) § Self-issued JWT.

**Cost:** one Redis `EXISTS` per authenticated request. With `slowapi` already doing one Redis op per request and `cache_aside` doing one or two more, the marginal cost is rounding-error. At extreme scale, batch via a Redis pipeline in custom middleware.

**Not for everyone:** if you don't need revocation, skip this entirely. Plain stateless JWT is simpler.

## Use 4 — Single-process SSE fan-out (Pub/Sub)

For cross-pod broadcast we use NATS ([`websockets.md`](websockets.md)). But for **within-process SSE fan-out** — one publisher, multiple SSE subscribers connected to the same pod — Redis pub/sub is a reasonable choice when NATS would be overkill:

```python
# services/notifications.py
async def stream_notifications(r: redis.Redis, *, user_id: UUID) -> AsyncGenerator[bytes, None]:
    """SSE generator — subscribes to one user's notification channel."""
    pubsub = r.pubsub()
    await pubsub.subscribe(f"notifications:{user_id}")
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                yield f"data: {msg['data']}\n\n".encode()
    finally:
        await pubsub.unsubscribe(f"notifications:{user_id}")
        await pubsub.aclose()
```

**Honest limitation:** Redis pub/sub is **fire-and-forget** — a subscriber that disconnects misses messages sent while gone. For at-least-once delivery (notifications you can't lose), use NATS JetStream and pay the operational cost. The streaming response pattern itself lives in [`streaming.md`](streaming.md).

## Use 5 — Distributed lock for "at most one worker"

Occasionally you need "only one pod runs the daily reconciliation job." Redis `SET key value NX PX` is the standard pattern:

```python
# core/redlock.py — simplified single-node lock (good enough for the daily-job case)
import secrets
import redis.asyncio as redis
from contextlib import asynccontextmanager


@asynccontextmanager
async def lock(r: redis.Redis, *, name: str, ttl_ms: int = 60_000):
    token = secrets.token_hex(16)
    acquired = await r.set(f"lock:{name}", token, nx=True, px=ttl_ms)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        # Only release if we still own the lock (Lua script for atomicity in real code).
        if await r.get(f"lock:{name}") == token:
            await r.delete(f"lock:{name}")
```

```python
async with lock(app.state.redis, name="daily-reconcile", ttl_ms=30 * 60 * 1000) as got:
    if got:
        await run_reconciliation()
```

**Caveats:** the single-node `SET NX PX` pattern is not safe under Redis failover. For true correctness (financial, exactly-once) use a Dapr Workflow ([`microservices.md`](microservices.md) § Dapr + Kubernetes interplay) or Postgres advisory locks. The Redis lock is a *good-enough* tool for the "don't run two of these at once" use case where double execution is annoying but not catastrophic.

## What we do NOT use Redis for

These are explicit no's — each has a documented alternative we prefer:

| Tempting use | Why we don't | What to use instead |
| ------------ | ------------ | ------------------- |
| **Sessions** (random session ID → user data) | Forces a Redis lookup on every authenticated request; revocation list achieves the same with stateless JWT | Stateless JWT + `jti` revocation list (above) |
| **Response-caching middleware** | Auth/locale leaks, streaming bodies break, `Vary` ignored, invalidation has nowhere to hook | Service-method `cache_aside` ([`cache.md`](cache.md)); CDN for public responses |
| **Cross-service event bus** | Two brokers to operate; Redis pub/sub has no persistence or consumer groups | NATS JetStream ([`microservices.md`](microservices.md)) |
| **Reliable background jobs** (Celery, ARQ, RQ) | We use durable workflows that survive pod crashes; no second broker | Dapr Workflow (`python-infrastructure` § dapr-workflows) or NATS JetStream consumers ([`microservices.md`](microservices.md)) |
| **Redis Streams as primary event log** | Same as cross-service event bus — NATS JetStream does the same with our existing infra | NATS JetStream consumer groups |
| **Storing PII** (user emails, addresses, etc.) | Redis isn't your durable database; eviction can drop it silently | Postgres + cache the projection if needed |
| **Writing custom rate-limit algorithms** | One bug away from letting everything through or blocking everything | `slowapi` (off-the-shelf) — [`rate-limiting.md`](rate-limiting.md) |
| **Hand-rolled cache decorators** | `cache_aside` inside service methods composes with auth/filters/post-processing; decorators don't | Service-method calls — [`cache.md`](cache.md) |

## Anti-patterns

- **Multiple Redis clients per process** (one for cache, one for rate limit, one for revocation) — wastes connection slots, complicates lifespan. **One `app.state.redis`**, all consumers share it.
- **`redis.from_url(...)` at module top** — connects at import, breaks tests, no cleanup. **Always in lifespan.**
- **No `await redis.ping()` after build** — silent failure mode where the app boots "successfully" but every request 500s on first Redis op. **Always ping in lifespan.**
- **Synchronous `redis` client** (`import redis` not `import redis.asyncio as redis`) inside `async def` routes — blocks the event loop. The async client is a drop-in replacement.
- **Storing JSON-encoded Python dicts** with no version field — schema evolution breaks every cached value at once. Use `model_dump_json()` from a Pydantic model and bump a `v` field when the schema changes.
- **Long-running `pubsub.listen()` loop inside a route without a timeout** — disconnect doesn't propagate cleanly; the coroutine leaks. Use `asyncio.wait_for(...)` or wrap with `finally: await pubsub.aclose()`.
- **Using `KEYS pattern`** — O(N) on the entire keyspace, blocks Redis. Always `SCAN` (`async for key in r.scan_iter(match="pattern:*")`).
- **`pickle` for cached values** — RCE if the cache is ever populated from untrusted input. Use `model_dump_json()` / `model_validate_json()` end-to-end.
- **No TTL on cached values** — Redis OOMs eventually; eviction policy decides what to drop, badly. **Every key gets an `ex=` or `px=`.**
- **Using Redis as the source of truth for anything you can't recompute** — eviction will eventually delete it; persistence isn't strong enough for financial / compliance data. Redis is a **cache and a coordination primitive**, not a database.
