# Caching (Redis)

This project uses **Redis** as the cache for hot reads and as a coordination point for short-lived state (rate limits, dedup windows, single-flight). Use `redis.asyncio` for any async service.

> Project-specific Redis recipes (e.g. session shape, namespace conventions, eviction policy) will land here once examples are in place. The patterns below are the baseline.

## Contents

- Async client setup
- Read-through cache
- Invalidate on write
- Cache stampede protection (single-flight)
- Rate limiting (token bucket)
- Dedup window (idempotency)
- Don't cache what changes faster than you can serve it
- Summary
- Gotchas

## Async client setup

```python
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

pool = ConnectionPool.from_url(
    "redis://redis:6379/0",
    max_connections=50,
    decode_responses=True,
)
redis = Redis(connection_pool=pool)
```

Wire it in a FastAPI app via a lifespan + `Depends` (see the `fastapi` skill).

## Read-through cache

```python
import json
from pydantic import BaseModel
from redis.asyncio import Redis

class User(BaseModel):
    id: str
    name: str
    email: str


async def get_user(user_id: str, redis: Redis, repo: UserRepository) -> User:
    key = f"user:{user_id}"
    cached = await redis.get(key)
    if cached is not None:
        return User.model_validate_json(cached)

    user = await repo.get_by_id(user_id)
    if user is not None:
        await redis.set(key, user.model_dump_json(), ex=300)  # 5-minute TTL
    return user
```

**Always set a TTL.** Never let cache entries live forever; stale data is worse than a re-fetch.

## Invalidate on write

```python
async def update_user(user_id: str, patch: UserPatch, redis: Redis, repo: UserRepository) -> User:
    user = await repo.update(user_id, patch)
    await redis.delete(f"user:{user_id}")
    return user
```

Use a consistent key shape (`{namespace}:{entity}:{id}`) so invalidation is predictable.

## Cache stampede protection (single-flight)

When a hot key expires, N concurrent requests all miss and all recompute. Mitigate with a short-lived lock:

```python
import asyncio
import uuid

async def get_with_singleflight(
    key: str,
    redis: Redis,
    compute: callable,
    ttl: int = 300,
    lock_ttl: int = 10,
) -> str:
    cached = await redis.get(key)
    if cached is not None:
        return cached

    lock_key = f"lock:{key}"
    lock_token = str(uuid.uuid4())
    got_lock = await redis.set(lock_key, lock_token, nx=True, ex=lock_ttl)

    if not got_lock:
        # Wait briefly for the holder to populate the cache
        for _ in range(20):
            await asyncio.sleep(0.05)
            cached = await redis.get(key)
            if cached is not None:
                return cached
        # Holder died or took too long — fall through and recompute
        return await compute()

    try:
        value = await compute()
        await redis.set(key, value, ex=ttl)
        return value
    finally:
        # Only release if we still own the lock (token check via Lua avoids races)
        await _release_lock(redis, lock_key, lock_token)


_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

async def _release_lock(redis: Redis, key: str, token: str) -> None:
    await redis.eval(_RELEASE_SCRIPT, 1, key, token)
```

## Rate limiting (token bucket)

```python
async def allow_request(key: str, redis: Redis, *, max_per_minute: int) -> bool:
    """Sliding-window-ish counter. Returns True if request is allowed."""
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, 60)
    count, _ = await pipe.execute()
    return count <= max_per_minute
```

For production-grade limiting (token bucket with refill, leaky bucket), use a Lua script that atomically reads + computes + writes — see Redis docs.

## Dedup window (idempotency)

```python
async def claim_idempotency_key(redis: Redis, key: str, ttl: int = 86400) -> bool:
    """Return True if we're the first to claim this key (good to proceed)."""
    return bool(await redis.set(f"idempotency:{key}", "1", nx=True, ex=ttl))
```

Pair with the `python-infrastructure/References/background-jobs.md` idempotency patterns.

## Don't cache what changes faster than you can serve it

Cache TTL must be shorter than the acceptable staleness, longer than the typical re-fetch interval. Keep them separate from object lifetimes — don't tie cache TTL to JWT expiry, session duration, or business deadlines.

## Summary

1. **`redis.asyncio` only** for async services.
2. **Always set a TTL.**
3. **Consistent key shape** — `{namespace}:{entity}:{id}`.
4. **Invalidate on write** — `DEL` the key before returning success.
5. **Single-flight** for hot keys to prevent stampedes.
6. **Lua scripts** for any read-then-write that must be atomic.
7. **Don't cache user-specific data with shared keys** — namespace by user where appropriate.
8. **Monitor hit rate** — if it's not high enough to matter, the cache is just adding latency on the miss path.

## Gotchas

- **`SETNX` + manual `EXPIRE` is a race** — the process can die between them, leaving a lock forever. Always use `SET NX EX` atomically.
- **Releasing a lock you no longer own** can release someone else's — verify the token via Lua before `DEL`.
- **Decoded vs binary responses** — `decode_responses=True` returns `str`; without it you get `bytes`. Pick one for the whole app.
- **`MULTI`/`EXEC` is not equivalent to a Lua script** — pipelined commands don't see each other's results. Use Lua for true atomic read-modify-write.
- **Cluster mode requires keys in the same hash slot** for multi-key operations — use hash tags `{user:42}:profile` to colocate related keys.
