# FastAPI Anti-Patterns

Quick-reference table of common mistakes. Scan this first when reviewing a diff.

For richer **code-level wrong-vs-right** examples on a given topic, the per-topic reference owns its own anti-pattern section:

| Topic                | Where to find deeper context                                     |
| -------------------- | ---------------------------------------------------------------- |
| DB / pooling         | [`database.md`](database.md) § Anti-patterns                     |
| Pagination           | [`pagination.md`](pagination.md) § Anti-patterns                 |
| Exception handlers   | [`exception-handlers.md`](exception-handlers.md) § Rules         |
| Files / uploads      | [`file-handling.md`](file-handling.md) § Anti-patterns           |
| Kubernetes / probes  | [`kubernetes.md`](kubernetes.md) § What NOT to do                |
| Observability / OTel | [`observability.md`](observability.md) § What NOT to do          |
| Authz / OpenFGA      | [`authz.md`](authz.md) (operational notes)                       |
| Dependencies         | [`dependencies.md`](dependencies.md) (rules in each subsection)  |
| Lifespan / middleware| [`production-patterns.md`](production-patterns.md) § Patterns to avoid |

## The table

| Anti-pattern                                                          | Why it's wrong                                                  | Fix                                                                                          |
| --------------------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `requests.get(...)` inside `async def`                                | Blocks the event loop — `requests` is sync.                     | `httpx.AsyncClient` or `await run_in_threadpool(requests.get, ...)`.                         |
| `time.sleep`, `open()`, sync DB driver inside `async def`             | Same — blocks the loop, kills throughput.                       | Async equivalent: `asyncio.sleep`, `aiofiles`, async DB driver.                              |
| `from jose import jwt`                                                | `python-jose` is largely unmaintained.                          | `import jwt` (PyJWT).                                                                        |
| `from async_asgi_testclient import TestClient`                        | Unmaintained.                                                   | `httpx.AsyncClient` + `ASGITransport`.                                                       |
| `model_config = ConfigDict(json_encoders={...})`                      | Deprecated in Pydantic v2.                                      | `@field_serializer` or `Annotated[T, PlainSerializer(...)]`.                                 |
| `Field(ge=18, default=None)`                                          | Constraint contradicts the default.                             | Pick: required (`Field(ge=18)`) or optional (`int \| None = Field(default=None, ge=18)`).    |
| `def get_user(id: int = Depends(...))` (default-arg form)             | Legacy; default-value gotchas.                                  | `user: Annotated[User, Depends(...)]`.                                                       |
| `except Exception:` around a route body                               | Hides bugs; turns 500s into silent 200s.                        | Catch the specific exception; raise `HTTPException` with a meaningful status.                |
| `BackgroundTasks` for anything you'd page on                          | No retry, dies with the worker.                                 | Use a real queue: **NATS JetStream** (background-jobs.md) or **Dapr Workflow** for multi-step sagas (dapr-workflows.md). Both in `python-infrastructure`. |
| Sync ORM session inside `async def`                                   | Blocks the loop; can deadlock the pool.                         | `AsyncSession` (SQLAlchemy 2.0 async) / `SQLModel` async.                                    |
| Return a Pydantic model AND set `response_model=` to the same class   | Model gets constructed twice (validate + serialize).            | Either return the ORM row / dict and let `response_model` validate, or drop `response_model`. |
| Deep cross-domain imports (`from src.auth.service.user import ...`)   | Tight coupling, hard to refactor.                               | `from src.auth import service as auth_service`.                                              |
| One `BaseSettings` for the whole app                                  | Every domain reads every var; nothing is scoped.                | One `BaseSettings` per domain (`AuthConfig`, `DbConfig`, …).                                 |
| Mocking the database in integration tests                             | Mock/prod divergence eventually fires in prod.                  | Real DB via testcontainers + ephemeral schema; `dependency_overrides` for auth.              |
| `RootModel[...]` for a typed payload                                  | Unnecessary indirection; FastAPI handles bare type aliases.     | `Annotated[list[int], Field(min_length=1), Body()]`.                                         |
| `@app.api_route("/x", methods=["GET", "POST"])`                       | Mixes operations; bad OpenAPI; if/else inside.                  | One function per HTTP operation.                                                             |
| Global mutable state (`db_session = None` at module top)              | Worker-local, races, leaks across requests.                     | Dependency injection; `app.state` set in lifespan.                                           |
| `engine = create_engine(...)` at import time                          | Connects during import; breaks tests; no cleanup.               | Build engines in lifespan, dispose on shutdown.                                              |
| Routes that catch their own exceptions and re-shape responses         | Scatters error-handling across the codebase.                    | Domain exceptions + `@app.exception_handler(...)`.                                           |
| `@asynccontextmanager` lifespan with no cleanup after `yield`         | Pools / clients leak on shutdown.                               | Always `await ...close()` / `await ...dispose()` after `yield`.                              |
| `ORJSONResponse` / `UJSONResponse`                                    | Deprecated in modern FastAPI; Pydantic-v2 already serializes in Rust. | Declare a return type or `response_model`. Don't override the response class for speed. |
| `dataclass` for a request/response model                              | No validation, no JSON schema, no FastAPI integration.          | Pydantic `BaseModel`. (Project-wide rule — see `writing-python`.)                            |
| Long-lived `httpx.AsyncClient()` created per-request                  | Re-opens the connection pool on every call; ~10× slowdown.      | One client per app, stored in `app.state`, created in lifespan.                              |
| Logging arbitrary user input at INFO without redaction                | PII / tokens leak to log sinks.                                 | Redact at the formatter or never log secrets; route logs through OTel (see `otel` skill).    |
| `print(...)` for diagnostics                                          | No structure, no log level, no correlation id.                  | `logging.getLogger(__name__)`; OTel forwards records.                                        |
| Authentication middleware that blocks routes globally                 | Couples auth to every endpoint; breaks public routes; can't reuse the resolved user. | Use a FastAPI dependency (`CurrentUserDep`); apply at the router level with `dependencies=[...]`. See `authn.md`. |
| Custom middleware that catches `Exception` and returns JSON           | Duplicates `@app.exception_handler`; scattered error shape.     | Use `@app.exception_handler(DomainError)` (see `production-patterns.md` § exception handlers). |
| `RequestValidationMiddleware` that re-checks Content-Type / body size | FastAPI + Pydantic already validate the body; Starlette has limits. | Push validation into the Pydantic model; configure `request.scope["app"]` size limits at the ASGI server. |
| `httpx.AsyncClient()` inside a middleware's `dispatch`                | New connection pool per request — defeats the point.            | One `app.state.http` client built in lifespan; inject via dep.                                |
| Setting a `ContextVar` in middleware without resetting in `finally`   | Value leaks into the next request that reuses the worker.       | `token = ctx.set(...)` → `ctx.reset(token)` in `finally`.                                    |
| Doing slow / blocking work in middleware                              | Middleware runs on every request; one slow call multiplies.     | Fire-and-forget (`asyncio.create_task`) or move to a background worker (`python-infrastructure`). |
| `span.record_exception(e)` in a route or handler                      | Deprecated in OTel — span events are second-class.              | Log the exception (`log.exception(...)`) inside the active span context — `trace_id`/`span_id` ride along automatically. See `observability.md` + `otel` skill. |
| Wrapping the whole route in `tracer.start_as_current_span`            | Duplicates the auto-created HTTP span; two layers of noise.     | Only wrap the **business operation** inside the route; let `FastAPIInstrumentor` own the HTTP span. |
| SDK-side sampling (`TraceIdRatioBased(0.1)` in `TracerProvider`)      | Drops traces before you know if the request errored.            | `AlwaysOn` at the SDK; tail-sample in the Collector. See `otel` `references/collector.md`. |
| Hard-coded OTLP endpoint in code                                      | Doesn't survive multi-env deploys.                              | `OTEL_EXPORTER_OTLP_ENDPOINT` env var.                                            |
| Running FastAPI with `--workers N` inside a Kubernetes pod            | Multiplies pools / clients / model loads per pod; lifespan runs N times invisibly to the scheduler. | One worker per pod, `replicas: N` in the Deployment. See `production-patterns.md` § Deployment & scaling. |
| No rate limit on `/token` / `/login` / `/forgot-password`             | Credential stuffing brute-forces unmetered.                     | `slowapi` or `fastapi-limiter` on auth endpoints (e.g. 5 / min / IP). Apply at the route or router, not as global middleware. |
| Reading `await request.body()` (or `request.json()`) in middleware    | The request body is a one-shot stream; consuming it in middleware leaves the route handler with an empty body and an `Empty body` error. | If you must inspect the body, read it once and store on `request.state`, OR use an ASGI `receive` wrapper to re-yield the bytes. Usually: just don't read the body in middleware. |
| Pure ASGI middleware that doesn't check `scope["type"]`               | Crashes on WebSocket / lifespan messages it wasn't designed for. | Always `if scope["type"] != "http": await self.app(scope, receive, send); return` at the top of `__call__`. |

## When in doubt

- "Will this run blocking I/O?" → use `def`, not `async def`.
- "Is this validation logic?" → push it into a Pydantic field or a dependency, not the route body.
- "Do I need to catch this exception here?" → almost always no; raise a domain exception and let a handler shape the response.
- "Should this be a global?" → no. Build it in lifespan and stash on `app.state`, then inject.
- "Will this work if the worker restarts mid-task?" → if not, it doesn't belong in `BackgroundTasks`.
