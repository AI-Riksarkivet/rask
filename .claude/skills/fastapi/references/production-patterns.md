# FastAPI Production Patterns

Lifespan, middleware, and the operational glue around a production FastAPI service.

> Defers JSON serialization, blocking-call rules, and HTTP-verb design to the parent SKILL.md. This file is strictly about **what the app does on startup, every request, and shutdown**. Health endpoints live in [`health-checks.md`](health-checks.md); database pooling in [`database.md`](database.md); k8s deployment in [`kubernetes.md`](kubernetes.md).

## Contents

- Lifespan — build once, dispose once
- Graceful shutdown — the complete pattern + patterns to avoid
- Bridging sync ↔ async with Asyncer
- Dependency injection from `app.state` — `SessionDep`, `HttpDep` wrappers
- Middleware — three styles, execution order, Request ID, Timing, Logging, built-in helpers, ContextVar
- Health checks → [`health-checks.md`](health-checks.md)
- Database connection pooling → [`database.md`](database.md)
- Hiding docs in production
- Exception handlers → [`exception-handlers.md`](exception-handlers.md)
- Key decisions

## Lifespan: build everything once, dispose everything once

Use `@asynccontextmanager` lifespan, not the deprecated `@app.on_event("startup"/"shutdown")` decorators.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
import httpx

from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    app.state.db_engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=settings.DB_POOL_SIZE,
        pool_pre_ping=True,
    )
    app.state.http = httpx.AsyncClient(timeout=15.0)
    # Optional: Redis client for caching — see `cache.md`.
    # app.state.redis = make_redis(); await app.state.redis.ping()

    # Fail fast if a dependency is unreachable.
    async with app.state.db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    yield  # ── app runs ──

    # ── shutdown ──
    await app.state.http.aclose()
    await app.state.db_engine.dispose()


app = FastAPI(lifespan=lifespan)
```

### Rules

- **One engine, one HTTP client, per process.** Created in lifespan, stashed on `app.state`. Never `create_async_engine` or `httpx.AsyncClient()` inside a route.
- **Liveness check during startup.** Cheap query / ping. Fail fast — a crash-loop is more honest than a half-broken app.
- **Always clean up after `yield`.** Pools leak on shutdown otherwise. Catching `BaseException` is unnecessary; the lifespan's `finally` happens implicitly via the context manager.
- **Don't put long-running tasks here.** Lifespan is for resource init, not background processing. Use a worker process (see `python-infrastructure`).

## Graceful shutdown — the complete pattern

This project's pattern is intentionally minimal because **uvicorn already does almost all of it**. The lifespan IS the shutdown handler. Below is the full code; the anti-pattern table at the end explains what we deliberately *don't* add.

### What uvicorn does for free on `SIGTERM`

1. Stop accepting new TCP connections.
2. Let in-flight requests finish (up to `--timeout-graceful-shutdown`, default 30 s).
3. Run your lifespan's post-`yield` block.
4. Exit.

You don't write a signal handler. You don't write a connection-draining tracker. You don't write a 503-returning middleware. uvicorn covers (1) and (2); the lifespan covers (3).

### The complete lifespan

```python
# main.py
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.health import router as health_router
from app.api.main import api_router
from app.core.config import settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ─── startup ───
    app.state.startup_complete = False
    app.state.shutting_down = False

    app.state.db_engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    app.state.http = httpx.AsyncClient(timeout=15.0)

    # Fail-fast liveness on startup — crash-loop > silent half-broken.
    async with app.state.db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    app.state.startup_complete = True
    log.info("startup_complete")

    yield  # ─── app serves traffic ───

    # ─── shutdown (uvicorn already waited for in-flight requests) ───
    app.state.shutting_down = True
    log.info("shutdown_begin")

    # Order matters: outermost first (clients we initiated), DB last
    # so that any final commit/rollback can still succeed.
    await app.state.http.aclose()
    await app.state.db_engine.dispose()

    log.info("shutdown_complete")


app = FastAPI(lifespan=lifespan)
app.include_router(health_router)                          # /livez, /readyz
app.include_router(api_router, prefix=settings.API_V1_STR)
```

### How readiness participates

The `/readyz` endpoint (see § Health checks) reads `app.state.startup_complete` and `app.state.shutting_down` — that's the entire "503 during shutdown" mechanism. No middleware required.

```python
# excerpt from api/health.py — see § Health checks for the full version
@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    state = request.app.state
    if not state.startup_complete:
        return JSONResponse(503, content={"status": "starting"})
    if state.shutting_down:
        return JSONResponse(503, content={"status": "shutting_down"})
    # ...component checks...
```

### How k8s participates

`preStop: sleep 5` gives the Service endpoints controller time to deregister the pod before SIGTERM, so the load balancer stops routing **before** uvicorn stops accepting. `terminationGracePeriodSeconds: 40` gives the chain enough budget. Full YAML in [`kubernetes.md`](kubernetes.md).

```
LB stops routing      ← preStop sleep 5s
   │
SIGTERM to uvicorn    ← uvicorn refuses new connections, waits for in-flight
   │
Lifespan post-`yield` ← your cleanup runs: http.aclose(), db.dispose()
   │
exit(0)
```

### Patterns to avoid

| Pattern                                                        | Why it's wrong                                                                                                                                  |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `loop.add_signal_handler(SIGTERM, ...)` in lifespan            | uvicorn already installs one. Adding another races: yours sets a flag uvicorn doesn't see, uvicorn closes the loop before yours runs, or you swallow the signal entirely. |
| `signal.signal(SIGTERM, ...)` at module level                  | Replaces uvicorn's handler. Shutdown degrades to "wait for SIGKILL".                                                                            |
| Custom `GracefulShutdownManager` class with priority handlers  | Ordered `await x.close()` calls in lifespan post-`yield` give you ordered cleanup. A registry adds ceremony for no win.                         |
| Custom `track_request()` middleware + in-flight counter        | uvicorn's `--timeout-graceful-shutdown` already waits for in-flight requests.                                                                   |
| Middleware returning 503 during shutdown                       | `/readyz` returning 503 + the LB deregistering already stops new traffic at the Service layer. Middleware adds a branch on every normal request. |
| `atexit.register(cleanup)`                                     | Doesn't run reliably under uvicorn's worker model or after SIGTERM. Lifespan post-`yield` is the supported hook.                                |
| Global `shutdown_manager` singleton                            | Module-level state breaks tests and worker isolation. Use flags on `app.state`.                                                                 |

If you reach for any of these, uvicorn isn't doing what you think — measure first. Likely real causes: short `terminationGracePeriodSeconds`, missing `preStop` sleep (so traffic still arrives during SIGTERM), or a route slower than the grace period (move that work to a background worker — see `python-infrastructure`).

## Bridging sync ↔ async with Asyncer

When you must call blocking code from an async handler, **don't** import `anyio.to_thread.run_sync` directly — use `asyncer.asyncify()`. Same primitive, friendlier API. The inverse (`syncify()`) lets a sync context call async code without spinning up a fresh event loop.

```python
from asyncer import asyncify, syncify
from fastapi import APIRouter

router = APIRouter()


def blocking_call(name: str) -> str:
    # legacy SDK with no async client, slow CPU work, etc.
    return f"hello {name}"


@router.get("/items/")
async def read_items() -> dict[str, str]:
    return {"message": await asyncify(blocking_call)(name="world")}
```

Prefer `asyncer` over raw `anyio` / `asyncio.to_thread` for readability; over `concurrent.futures.ThreadPoolExecutor` because it integrates with Starlette's existing threadpool budget (default 40 workers — saturating it slows every sync route).

## Dependency injection from `app.state`

Stashed clients are retrieved through a small dep wrapper, never imported directly from `app.state` in route code.

```python
# api/deps.py
from collections.abc import AsyncGenerator
from typing import Annotated
from fastapi import Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession
import httpx


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(request.app.state.db_engine, expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


SessionDep = Annotated[AsyncSession, Depends(get_db)]
HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
```

Routes consume `SessionDep` / `HttpDep` — no knowledge of `app.state`.

## Middleware

### Three styles — pick the smallest that fits

| Style                                                       | When to use                                                                                                  | Cost                                                                |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| **`@app.middleware("http")` decorator**                     | One-off, no config, no state. Smallest amount of code.                                                       | Hard to test in isolation; can't be packaged for reuse.             |
| **`BaseHTTPMiddleware` subclass**                           | Reusable, configurable (constructor args), shareable across services. **Default choice** for this project.   | Slight overhead vs pure ASGI.                                       |
| **Pure ASGI (`async def __call__(self, scope, receive, send)`)** | Maximum performance, need to inspect/modify ASGI messages directly, or stream bodies without buffering. | Verbose, easy to break; only reach for it when you have measured.   |

> If you're catching yourself implementing both — class-based AND a decorator — you're doing too much. Pick one.

### Execution order (onion)

FastAPI runs middleware in **reverse** of registration order on the way in, then re-reverses on the way out. The last `add_middleware(...)` call wraps everything else and runs first.

```
register order:        execution per request:
  1. CORS                CORS  ──┐  outermost
  2. RequestID             RequestID ──┐
  3. Timing                  Timing ──┐
  4. Logging                   Logging ──┐
                                   Route handler
                                   (then unwinds in reverse)
```

Recommended order (registered in this order):

1. CORS
2. Request ID
3. Timing
4. Logging

```python
# main.py
from fastapi.middleware.cors import CORSMiddleware
from app.middleware import RequestIDMiddleware, TimingMiddleware, LoggingMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Response-Time"],
)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(TimingMiddleware)
app.add_middleware(LoggingMiddleware)
```

### Request ID

```python
# middleware.py
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

### Timing

```python
import time
from starlette.middleware.base import BaseHTTPMiddleware


class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Response-Time"] = f"{(time.perf_counter() - start):.3f}s"
        return response
```

### Logging

Use stdlib `logging` (project standard — OTel auto-instrumentation picks it up; see the `otel` skill). Don't add `structlog`.

```python
import logging
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("app.requests")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = getattr(request.state, "request_id", "-")
        try:
            response = await call_next(request)
            log.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                },
            )
            return response
        except Exception:
            log.exception(
                "request_failed",
                extra={"request_id": request_id, "method": request.method, "path": request.url.path},
            )
            raise
```

`extra={...}` keys land as span attributes when OTel logging instrumentation is enabled. Same data, no extra dep.

### Built-in middleware worth knowing

Don't roll your own when these exist:

```python
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

app.add_middleware(GZipMiddleware, minimum_size=500)  # compress > 500B
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["api.example.com", "*.example.com"])
app.add_middleware(HTTPSRedirectMiddleware)  # only behind a TLS-terminating proxy
```

For security headers (CSP, X-Frame-Options, Referrer-Policy), a tiny `BaseHTTPMiddleware`:

```python
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response
```

### Request-scoped data via `ContextVar`

To thread the request ID (or current user, trace id, tenant) through code that doesn't take `request` as a parameter — log records, background helpers, repository methods — use a `ContextVar`. It's per-request, asyncio-safe, no globals.

```python
# core/context.py
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return request_id_ctx.get()
```

```python
# middleware.py
class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_ctx.reset(token)  # critical — without this, ids leak across requests in worker reuse
```

Anywhere downstream, `current_request_id()` returns the correct value without plumbing.

## Health checks

See [`health-checks.md`](health-checks.md) — `/livez` and `/readyz` endpoints with healthy / degraded / unhealthy states, per-component reporting, and the `app.state.startup_complete` / `shutting_down` flags wired in from the lifespan.

## Database connection pooling

See [`database.md`](database.md) — SQLModel + asyncpg + `AsyncEngine` setup, pool sizing formula, the flags that actually matter (`pool_pre_ping`, `pool_recycle`), and when PgBouncer pays off.

## Hiding docs in production

```python
SHOW_DOCS_IN = {"local", "staging"}

app_kwargs: dict[str, object] = {
    "title": settings.PROJECT_NAME,
    "lifespan": lifespan,
}
if settings.ENVIRONMENT not in SHOW_DOCS_IN:
    app_kwargs["openapi_url"] = None  # disables /docs, /redoc, /openapi.json

app = FastAPI(**app_kwargs)
```

Auth-protect `/docs` instead of hiding it only if internal consumers need it in prod — set `docs_url=None` and mount a private router with the OpenAPI JSON behind an auth dep.

## Exception handlers

See [`exception-handlers.md`](exception-handlers.md) — `DomainError` hierarchy, RFC 9457 Problem Details responses, `RequestValidationError` override for clean 422s, and `RateLimitError` with the required `Retry-After` header. Routes raise domain exceptions; one handler per class, registered in `main.py`.

## Key decisions

| Decision           | Recommendation                                                  |
| ------------------ | --------------------------------------------------------------- |
| Startup / shutdown | `@asynccontextmanager` lifespan, not `on_event`                 |
| Resource location  | `app.state` (set in lifespan), accessed via DI                  |
| Dependencies       | `Annotated[T, Depends(...)]` aliases                            |
| Settings           | One `BaseSettings` per domain (`AuthConfig`, `DbConfig`, …)     |
| Response class     | FastAPI default — don't reach for `ORJSONResponse`              |
| Logging            | stdlib `logging` + OTel auto-instrumentation; no `structlog`    |
| Middleware order   | CORS → Request ID → Timing → Logging                            |
| Health             | Split `livez` (no deps) and `readyz` (deps)                     |
| Error mapping      | Global `@app.exception_handler` for domain exceptions           |
| Scaling on k8s     | One worker per pod; scale via `replicas`, not `--workers N`     |
| Shutdown on k8s    | `preStop: sleep 5` + 30s `terminationGracePeriodSeconds`        |
