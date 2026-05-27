# Rate Limiting

What to put on `/login`, `/token`, `/forgot-password`, and any expensive route. Use **`slowapi`** with the **`app.state.redis`** client built in [`cache.md`](cache.md) — never a global middleware, never write your own algorithm.

## Contents

- Choosing `slowapi`
- Setup — Redis-backed limiter on `app.state`
- Per-route limits
- Per-router shared limits
- Keying — IP, user, API key
- Custom 429 response (RFC 9457 Problem Details + `Retry-After`)
- Anti-patterns

## Choosing `slowapi`

`slowapi` is the project standard:

- Battle-tested wrapper around the `limits` library — supports fixed-window, moving-window, and sliding-window algorithms out of the box
- Native FastAPI integration (`@limiter.limit(...)` decorator + `Request` dep)
- Redis-backed for multi-pod consistency; falls back to in-memory for single-pod / tests
- Emits `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After` headers automatically

**Don't write your own.** The article-style sliding-window / token-bucket implementations break at the second pod (state in `defaultdict` is per-process). If you genuinely need bursts or load-aware throttling, that's an API-gateway concern (Envoy, Kong) — not in-process Python.

## Install

```bash
uv add slowapi redis
```

slowapi 0.x uses the **synchronous** `redis-py` client for its storage backend, not async — its internal strategies (`FixedWindowRateLimiter` etc.) are sync-only. The blocking rate-limit check is ~1 ms (one `INCR + EXPIRE`) and FastAPI handles it in the threadpool, so it's not a meaningful event-loop hazard. Don't use `async+redis://` URIs with slowapi 0.x — it fails at construction with `AssertionError` because the sync strategies reject async storage.

## Setup — Redis-backed limiter on `app.state`

slowapi opens its own (sync) redis-py connection, separate from your `app.state.redis` async client. They share the broker (one Redis cluster); the keys live alongside your cache keys under a `LIMITER/` prefix that slowapi sets automatically.

```python
# core/rate_limit.py
from slowapi import Limiter
from slowapi.util import get_remote_address


def make_limiter(redis_url: str) -> Limiter:
    # redis://... — sync backend; slowapi 0.x does NOT support async+redis://
    return Limiter(key_func=get_remote_address, storage_uri=redis_url)
```

```python
# main.py — lifespan
from slowapi.errors import RateLimitExceeded

from app.core.rate_limit import make_limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... db, http, redis ...
    app.state.limiter = make_limiter(str(settings.REDIS_URL))
    yield
```

```python
# api/deps.py — expose limiter as a dep (so tests can override it)
from typing import Annotated
from fastapi import Depends, Request
from slowapi import Limiter


def get_limiter(request: Request) -> Limiter:
    return request.app.state.limiter


LimiterDep = Annotated[Limiter, Depends(get_limiter)]
```

## Per-route limits

The single most useful pattern — apply to auth routes and expensive operations only:

```python
# api/routes/auth.py
from fastapi import APIRouter, Request
from slowapi import Limiter

from app.api.deps import limiter  # imported from app.state via dep

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
@limiter.limit("5/minute")              # 5 attempts per IP per minute
async def login(request: Request, payload: LoginRequest) -> Token:
    ...


@router.post("/forgot-password")
@limiter.limit("3/hour")                # tighter — emails cost real money
async def forgot_password(request: Request, payload: ForgotPasswordRequest) -> Message:
    ...
```

**`request: Request` is mandatory** — slowapi reads it to extract the key. Forget it and you get `RateLimitExceeded` raised on every call.

Rate-limit strings: `"5/minute"`, `"100/hour"`, `"10/second"`. See [`limits` docs](https://limits.readthedocs.io/en/stable/quickstart.html#rate-limit-string-notation) for the full grammar.

## Per-router shared limits

When every route in a router needs the same baseline:

```python
# api/routes/expensive.py — all routes share one quota
router = APIRouter(
    prefix="/exports",
    tags=["exports"],
    dependencies=[Depends(RateLimitDep)],  # see below
)
```

```python
# api/deps.py
async def rate_limit_dep(request: Request) -> None:
    # 20/hour for the whole /exports namespace, keyed by user not IP
    await request.app.state.limiter.check(request, "20/hour", key=request.state.user_id)


RateLimitDep = Annotated[None, Depends(rate_limit_dep)]
```

## Keying — IP, user, API key

The default `get_remote_address` keys by client IP — fine for anonymous endpoints, **wrong** for authenticated APIs (a corporate NAT shares one IP for thousands of users).

```python
# core/rate_limit.py — custom key functions
from fastapi import Request


def by_user(request: Request) -> str:
    # request.state.user is set by the authn dep (see authn.md)
    user = getattr(request.state, "user", None)
    return f"user:{user.id}" if user else f"ip:{request.client.host}"


def by_api_key(request: Request) -> str:
    key = request.headers.get("X-API-Key")
    return f"apikey:{key}" if key else f"ip:{request.client.host}"
```

```python
@limiter.limit("100/minute", key_func=by_user)
async def list_items(request: Request, user: CurrentUserDep) -> list[Item]:
    ...
```

**Always include an `ip:` fallback** so unauthenticated requests can't bypass the limit by omitting the header.

## Custom 429 response

Make the rate-limit response match the [RFC 9457 Problem Details](exception-handlers.md) shape used elsewhere in the app:

```python
# main.py
from slowapi.errors import RateLimitExceeded


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "type": "https://api.example.com/problems/rate-limit",
            "title": "Too Many Requests",
            "status": 429,
            "detail": f"Rate limit exceeded: {exc.detail}",
        },
        headers={"Retry-After": str(exc.headers.get("Retry-After", 60))},
        media_type="application/problem+json",
    )
```

slowapi auto-attaches `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After` to both 200 and 429 responses — clients can self-throttle without parsing the body.

## Anti-patterns

| Pattern | Why it's wrong | Fix |
| ------- | -------------- | --- |
| **Global middleware** applying one limit to every route | Breaks `/livez`, `/readyz`, `/docs`; one number can't be right for both `/login` and `/health` | Per-route `@limiter.limit(...)` |
| In-process `defaultdict` rate limiter (article-style) | State per process → with N pods you get N× the real limit | Redis-backed `slowapi` |
| Keying by `request.client.host` for authenticated endpoints | Corporate NAT shares one IP across thousands of users → all blocked together | Custom `key_func` returning `user:{id}` with IP fallback |
| Writing your own token bucket / sliding window / adaptive limiter | Always one bug away from either letting everything through or blocking everything; not your core problem | `slowapi` for in-process, API gateway (Envoy, Kong) for global |
| Forgetting `request: Request` in the route signature | slowapi can't extract the key → every call returns 429 | Always declare it on `@limiter.limit`-decorated routes |
| No rate limit on `/login`, `/token`, `/forgot-password` | Credential stuffing brute-forces unmetered; reset-password sends emails on every hit | `5/minute` IP + per-user account-lockout in the authn layer |
| Returning a plain `429` with no headers | Clients can't self-throttle, retry storms hammer the API back into 429 | slowapi handles this automatically; don't strip the headers |
| Rate-limiting health checks | k8s probes fail under load → pods cycle → real outage | Probe routes (`/livez`, `/readyz`) get no decorator, full stop |
