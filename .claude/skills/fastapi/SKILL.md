---
name: fastapi
description: FastAPI best practices, conventions, and production project templates. Use when writing or refactoring FastAPI APIs and Pydantic models, or when scaffolding a new FastAPI project with async patterns, dependency injection, repositories, services, auth, and tests.
---

# FastAPI

FastAPI is the project standard for HTTP services — Pydantic v2 native, async-first, autogenerates OpenAPI from type hints, threadpool fallback for sync handlers. **Flask / Django / Starlette-direct aren't used here** — pick FastAPI for any new HTTP service unless you're writing a non-HTTP CLI (use `typer` instead, see `writing-python`).

Routing index. Each topic links to a reference with full patterns and code. New projects start at [`project-template.md`](references/project-template.md); day-to-day code-style rules live in [`core-conventions.md`](references/core-conventions.md).

## Scope routing

| If you're working on… | Read |
| --------------------- | ---- |
| **Day-to-day route / model / DI style** (`Annotated`, no `...`, `response_model`, async vs sync, `StrEnum`, no `RootModel`, one HTTP op per function, `fastapi` CLI) | [`core-conventions.md`](references/core-conventions.md) |
| **Scaffolding a new project** — layered layout, settings, repository/service/endpoint patterns, auth wiring, tests | [`project-template.md`](references/project-template.md) |
| **Dependency injection** — `yield`/`scope`, sub-dependency chains, per-request caching + `use_cache=False`, class deps, `app.dependency_overrides` for testing | [`dependencies.md`](references/dependencies.md) |
| **Production wiring** — lifespan (`@asynccontextmanager`, never `on_event`), graceful shutdown, DI via `app.state`, middleware (CORS → RequestID → Timing → Logging), `ContextVar`, hiding `/docs` in prod | [`production-patterns.md`](references/production-patterns.md) |
| **Exception handlers** — `DomainError` hierarchy, RFC 9457 Problem Details, `RequestValidationError` override, `RateLimitError` + `Retry-After` | [`exception-handlers.md`](references/exception-handlers.md) |
| **Health checks** — `/livez` + `/readyz`, healthy / degraded / unhealthy states, per-component reporting, `startup_complete` / `shutting_down` flags from lifespan | [`health-checks.md`](references/health-checks.md) |
| **Database** — SQLModel + asyncpg + `AsyncEngine`, pool flags (`pool_pre_ping`, `pool_recycle`), sizing formula, PgBouncer, **Alembic migrations** (env.py for SQLModel, k8s `initContainer`, data-migration batching) | [`database.md`](references/database.md) |
| **Pagination** — offset / cursor / keyset, reusable `PaginationParams`, generic `Page[Item]` (PEP 695), filter+sort+pagination, index requirements, `MAX_OFFSET` | [`pagination.md`](references/pagination.md) |
| **Caching** — `cache_aside` for service methods, mutation→invalidation, lifespan warming, why-not response-cache middleware | [`cache.md`](references/cache.md) |
| **Redis (design choice + wiring hub)** — when to add Redis vs NATS/Postgres, shared `make_redis` + `RedisDep`, JWT `jti` revocation, single-process SSE pub/sub, distributed lock | [`redis.md`](references/redis.md) |
| **Rate limiting** — `slowapi` per-route (not global middleware), Redis-backed via `app.state.redis`, key by `user_id` not IP; mandatory on `/login`, `/token`, `/forgot-password` | [`rate-limiting.md`](references/rate-limiting.md) |
| **WebSockets** — authn BEFORE `accept()`, `ConnectionManager` on `app.state`, server-side heartbeat, NATS JetStream for horizontal scaling, manual OTel spans; prefer SSE for one-way push | [`websockets.md`](references/websockets.md) |
| **File handling** — `FileResponse` (path-traversal guard), `StreamingResponse` for generated content, HTTP-range for video / resumable, `UploadFile` chunked reads, temp-file cleanup | [`file-handling.md`](references/file-handling.md) |
| **Streaming** — JSON Lines, Server-Sent Events (`EventSourceResponse`, `ServerSentEvent`), byte streams | [`streaming.md`](references/streaming.md) |
| **Authentication** — _who is the request?_ — pwdlib (Argon2 + bcrypt), self-issued JWT with PyJWT, OIDC verification (Keycloak/Dex/Okta/Auth0/Entra/Google), protected-route patterns | [`authn.md`](references/authn.md) |
| **Authorization** — _what may they do?_ — coarse role/scope deps, **OpenFGA** (Zanzibar-style) for fine-grained relational permissions | [`authz.md`](references/authz.md) |
| **Observability** — FastAPI-specific OTel: `FastAPIInstrumentor`, `excluded_urls`, `server_request_hook`, when to wrap business ops manually. SDK setup defers to the `otel` skill | [`observability.md`](references/observability.md) |
| **Kubernetes deployment** — one worker per pod (never gunicorn), full Deployment YAML, PodDisruptionBudget, HPA on RPS, `terminationGracePeriodSeconds` math, `preStop` sleep, **Dapr sidecar interplay** cross-link | [`kubernetes.md`](references/kubernetes.md) |
| **Microservices** — when to split, one-DB-per-service, sync (httpx ≤2 hops) vs async (NATS JetStream or Dapr pub/sub on JetStream), outbox via Dapr Workflow, trace context propagation, service-to-service authn, contract sharing, **Dapr + Kubernetes interplay** | [`microservices.md`](references/microservices.md) |
| **Anti-patterns** — quick-lookup table of ~35 common mistakes (blocking I/O in `async def`, deprecated APIs, `engine = create_engine(...)` at import, mocking the DB, etc.) | [`anti-patterns.md`](references/anti-patterns.md) |

## OpenAPI / docs flags (the only inline rule)

Two per-route flags worth knowing inline (everything else auto-derives from types): **`include_in_schema=False`** hides internal endpoints from `/docs`; **`deprecated=True`** strikethroughs during a deprecation window. Prod-wide `docs_url=None` / `openapi_url=None` lives in [`production-patterns.md`](references/production-patterns.md).

## Tooling & libraries

Single source of truth in sibling skills + linked references: **`uv` / `ruff` / `ty`** (`writing-python` + `astral:*`), **`SQLModel`** over SQLAlchemy ([`database.md`](references/database.md)), **`HTTPX`** over requests (lifespan + `HttpDep` in [`production-patterns.md`](references/production-patterns.md)), **`Asyncer`** for sync↔async, **`PyJWT`** over python-jose, **`pwdlib`** over passlib ([`authn.md`](references/authn.md)).

## Cross-skill boundaries

- **`writing-python`** — language style, type safety, Pydantic, uv/ruff/ty. This skill — _what changes when the code is a FastAPI route_.
- **`python-infrastructure`** — NATS JetStream consumers, Dapr Workflow, retry/backoff, OTel conventions, Redis fundamentals. This skill links out for the cross-cutting concerns.
- **`otel`** — full OTel SDK reference. [`observability.md`](references/observability.md) only pins the FastAPI-specific bits on top.

## Top gotchas

- **Blocking I/O in `async def`** kills throughput. If in doubt use plain `def` — FastAPI runs it in a threadpool. See [`core-conventions.md`](references/core-conventions.md) § Async vs sync.
- **`engine = create_engine(...)` at import time** breaks tests and leaks resources. Always build in lifespan. See [`production-patterns.md`](references/production-patterns.md) § Lifespan.
- **`--workers N` inside a Kubernetes pod** multiplies pools and lifespan runs. One worker per pod, scale via `replicas:`. See [`kubernetes.md`](references/kubernetes.md) § Process model.
- **`BackgroundTasks` for anything you'd page on** dies with the worker. Use NATS JetStream or Dapr Workflow. See [`anti-patterns.md`](references/anti-patterns.md).
- **Authenticate WebSockets before `accept()`** — otherwise the socket is open and counted before you reject. See [`websockets.md`](references/websockets.md).
