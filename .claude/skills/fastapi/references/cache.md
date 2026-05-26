# Caching (FastAPI-specific)

What's different about caching *inside a FastAPI service*. Everything else — Redis connection-pool tuning, TTL strategies, invalidation patterns (TTL / events / tags / dependency), cache-warming algorithms, stale-while-revalidate — lives in the **`python-infrastructure`** skill (`References/caching.md`). Don't duplicate it here.

## Contents

- `RedisDep` from lifespan
- `cache_aside` helper for service methods
- Mutation → invalidation (the only correct moment to invalidate)
- Why **not** a response-caching middleware
- Lifespan cache-warming hook
- We use stateless JWT, not Redis sessions

## `RedisDep` from lifespan

Same pattern as `SessionDep` / `HttpDep` (see `production-patterns.md` § Lifespan + DI from `app.state`). One client per process, opened in startup, closed in shutdown.

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
        retry_on_timeout=True,
    ))
```

```python
# main.py — extend the lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... db_engine, http ...
    app.state.redis = make_redis()
    await app.state.redis.ping()           # fail-fast on bad URL / unreachable host
    yield
    await app.state.redis.aclose()
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

Routes consume `RedisDep` — never `request.app.state.redis` directly.

## `cache_aside` helper

Use cache inside **service methods**, not at the route boundary — same reason `check` from `authz.md` lives in services: you often need post-filtering, projection, or composition with auth.

```python
# core/cache.py
from collections.abc import Awaitable, Callable
from pydantic import BaseModel
import redis.asyncio as redis


async def cache_aside[Item: BaseModel](
    r: redis.Redis,
    *,
    key: str,
    ttl_s: int,
    model: type[Item],
    fetch: Callable[[], Awaitable[Item | None]],
) -> Item | None:
    """Get-or-fetch with Pydantic round-trip. Returns `None` if fetch returns None."""
    raw = await r.get(key)
    if raw is not None:
        return model.model_validate_json(raw)

    fresh = await fetch()
    if fresh is not None:
        await r.set(key, fresh.model_dump_json(), ex=ttl_s)
    return fresh
```

Used inside a service method:

```python
# services/users.py
async def get_user(session: AsyncSession, r: redis.Redis, *, user_id: UUID) -> User | None:
    return await cache_aside(
        r, key=f"user:{user_id}", ttl_s=300, model=User,
        fetch=lambda: session.get(User, user_id),
    )
```

**Never** wrap a whole route handler in a cache. The cache key would have to fold in every header / cookie / query param that affects the response (auth, locale, feature flags) — that's exactly what makes the response-caching middleware below an anti-pattern.

## Mutation → invalidation

The only correct moment to invalidate is **after a successful DB write**, in the same service method. One write path → one invalidation path.

```python
# services/users.py
from app.core.exceptions import NotFoundError


async def update_user(
    session: AsyncSession, r: redis.Redis, *, user_id: UUID, patch: UserPatch,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"user {user_id}")

    for k, v in patch.model_dump(exclude_unset=True).items():
        setattr(user, k, v)
    await session.commit()

    # Invalidate AFTER commit — never before. If the commit rolls back and we
    # already deleted the cache, the next read repopulates with the old row.
    await r.delete(f"user:{user_id}")
    return user
```

For patterns more sophisticated than `del key` (tag-based invalidation, dependency cascades, pub/sub fan-out across replicas), see `python-infrastructure` § caching — those are stack-level concerns, not FastAPI-specific.

## Lifespan cache-warming hook

If a cold cache causes thundering-herd at startup, warm critical keys *inside* the lifespan, before `yield`. Bound the warming time — k8s won't wait forever on a slow startup probe.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = make_redis()
    await app.state.redis.ping()

    # Optional — only for keys with bounded cardinality and high hit-rate.
    try:
        async with asyncio.timeout(10.0):
            await warm_popular_products(app.state.db_engine, app.state.redis)
    except TimeoutError:
        log.warning("cache_warming_timeout — starting without warm cache")

    app.state.startup_complete = True
    yield
    # ... shutdown ...
```

If warm-up takes longer than ~15 s, add a `startupProbe` to the Deployment (`kubernetes.md` § Full Deployment YAML).

## Why **not** a response-caching middleware

The "wrap every route response in Redis" middleware pattern (build a cache key from method + path + query, store the entire response body) looks tempting and is almost always wrong:

| Problem | Why                                                                                                                                |
| ------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Auth-aware responses leak | Cache key must include every auth claim / cookie / locale / feature-flag that affected the body. Get it wrong once and you serve user A's data to user B. |
| Streaming bodies break | `StreamingResponse` and `FileResponse` aren't buffer-cacheable without re-reading the entire stream first.                          |
| `Vary:` headers are ignored | The middleware has no idea what's in the body, so it can't honor `Vary: Accept`, `Vary: Authorization`, etc.                       |
| Invalidation has nowhere to hook | Mutations happen in handlers; the middleware sits one layer up and can't see them. You end up scanning patterns at the wrong layer.|
| Hides the cost | Routes that look fast in dashboards are actually paying a Redis round-trip on every request, *plus* the real fetch on every miss.   |

**Cache inside service methods** (above). Compose explicitly with auth (`authz.md`) and pagination (`pagination.md`) where you can reason about the key.

CDN-edge caching is a separate concern — that lives in your CDN / reverse proxy config, not the Python app.

## We use stateless JWT, not Redis sessions

Some Redis tutorials build a session manager (Redis keys keyed by random session ID, sliding TTL, cookie-based login). We don't:

- **Local auth** issues a self-signed JWT (`authn.md` § Self-issued JWT).
- **External IdP** verifies an OIDC token (`authn.md` § OIDC).
- Either way the token is stateless — no server-side session storage to invalidate. The Redis we already have is for *caching*, not user state.

If you genuinely need server-side session revocation (compliance, "log out everywhere"), add a **revocation list** in Redis keyed by `jti` claim — not a full session manager.
