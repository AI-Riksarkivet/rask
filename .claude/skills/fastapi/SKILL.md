---
name: fastapi
description: FastAPI best practices, conventions, and production project templates. Use when writing or refactoring FastAPI APIs and Pydantic models, or when scaffolding a new FastAPI project with async patterns, dependency injection, repositories, services, auth, and tests.
---

# FastAPI

Official FastAPI skill to write code with best practices, keeping up to date with new versions and features. Covers both day-to-day conventions for existing code and the layout/patterns for new projects.

## New Project Scaffolding

For a new FastAPI project, follow the layered layout (`api/` → `services/` → `repositories/` → `models/` + `schemas/`, plus `core/` for config/security/database) and the application entry, settings, repository, service, endpoint, auth, and testing patterns in [the project template reference](references/project-template.md). All examples there use the conventions in this skill (`Annotated` dependencies, no `...`, typed returns, one HTTP operation per function).

## Use the `fastapi` CLI

Run the development server on localhost with reload:

```bash
fastapi dev
```

Run the production server:

```bash
fastapi run
```

### Add an entrypoint in `pyproject.toml`

FastAPI CLI will read the entrypoint in `pyproject.toml` to know where the FastAPI app is declared.

```toml
[tool.fastapi]
entrypoint = "my_app.main:app"
```

### Use `fastapi` with a path

When adding the entrypoint to `pyproject.toml` is not possible, or the user explicitly asks not to, or it's running an independent small app, you can pass the app file path to the `fastapi` command:

```bash
fastapi dev my_app/main.py
```

Prefer to set the entrypoint in `pyproject.toml` when possible.

## Use `Annotated`

Always prefer the `Annotated` style for parameter and dependency declarations.

It keeps the function signatures working in other contexts, respects the types, allows reusability.

### In Parameter Declarations

Use `Annotated` for parameter declarations, including `Path`, `Query`, `Header`, etc.:

```python
from typing import Annotated

from fastapi import FastAPI, Path, Query

app = FastAPI()


@app.get("/items/{item_id}")
async def read_item(
    item_id: Annotated[int, Path(ge=1, description="The item ID")],
    q: Annotated[str | None, Query(max_length=50)] = None,
):
    return {"message": "Hello World"}
```

instead of:

```python
# DO NOT DO THIS
@app.get("/items/{item_id}")
async def read_item(
    item_id: int = Path(ge=1, description="The item ID"),
    q: str | None = Query(default=None, max_length=50),
):
    return {"message": "Hello World"}
```

### For Dependencies

Use `Annotated` for dependencies with `Depends()`.

Unless asked not to, create a new type alias for the dependency to allow re-using it.

```python
from typing import Annotated

from fastapi import Depends, FastAPI

app = FastAPI()


def get_current_user():
    return {"username": "johndoe"}


CurrentUserDep = Annotated[dict, Depends(get_current_user)]


@app.get("/items/")
async def read_item(current_user: CurrentUserDep):
    return {"message": "Hello World"}
```

instead of:

```python
# DO NOT DO THIS
@app.get("/items/")
async def read_item(current_user: dict = Depends(get_current_user)):
    return {"message": "Hello World"}
```

## Do not use Ellipsis for _path operations_ or Pydantic models

Do not use `...` as a default value for required parameters, it's not needed and not recommended.

Do this, without Ellipsis (`...`):

```python
from typing import Annotated

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field


class Item(BaseModel):
    name: str
    description: str | None = None
    price: float = Field(gt=0)


app = FastAPI()


@app.post("/items/")
async def create_item(item: Item, project_id: Annotated[int, Query()]): ...
```

instead of this:

```python
# DO NOT DO THIS
class Item(BaseModel):
    name: str = ...
    description: str | None = None
    price: float = Field(..., gt=0)


app = FastAPI()


@app.post("/items/")
async def create_item(item: Item, project_id: Annotated[int, Query(...)]): ...
```

## Return Type or Response Model

When possible, include a return type. It will be used to validate, filter, document, and serialize the response.

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str
    description: str | None = None


@app.get("/items/me")
async def get_item() -> Item:
    return Item(name="Plumbus", description="All-purpose home device")
```

**Important**: Return types or response models are what filter data ensuring no sensitive information is exposed. And they are used to serialize data with Pydantic (in Rust), this is the main idea that can increase response performance.

The return type doesn't have to be a Pydantic model, it could be a different type, like a list of integers, or a dict, etc.

### When to use `response_model` instead

If the return type is not the same as the type that you want to use to validate, filter, or serialize, use the `response_model` parameter on the decorator instead.

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str
    description: str | None = None


@app.get("/items/me", response_model=Item)
async def get_item() -> dict[str, object]:
    return {"name": "Foo", "description": "A very nice Item"}
```

This can be particularly useful when filtering data to expose only the public fields and avoid exposing sensitive information.

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class InternalItem(BaseModel):
    name: str
    description: str | None = None
    secret_key: str


class Item(BaseModel):
    name: str
    description: str | None = None


@app.get("/items/me", response_model=Item)
async def get_item() -> InternalItem:
    return InternalItem(
        name="Foo", description="A very nice Item", secret_key="supersecret"
    )
```

## Performance

Do not use `ORJSONResponse` or `UJSONResponse`, they are deprecated.

Instead, declare a return type or response model. Pydantic will handle the data serialization on the Rust side.

## OpenAPI / docs flags

`include_in_schema=False` hides internal endpoints; `deprecated=True` strikethroughs during a deprecation window. Prod-wide `docs_url=None` lives in [`production-patterns.md`](references/production-patterns.md).

## Including Routers

When declaring routers, prefer to add router level parameters like prefix, tags, etc. to the router itself, instead of in `include_router()`.

Do this:

```python
from fastapi import APIRouter, FastAPI

app = FastAPI()

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/")
async def list_items():
    return []


# In main.py
app.include_router(router)
```

instead of this:

```python
# DO NOT DO THIS
from fastapi import APIRouter, FastAPI

app = FastAPI()

router = APIRouter()


@router.get("/")
async def list_items():
    return []


# In main.py
app.include_router(router, prefix="/items", tags=["items"])
```

There could be exceptions, but try to follow this convention.

Apply shared dependencies at the router level via `dependencies=[Depends(...)]`.

## Dependency Injection

See [the dependency injection reference](references/dependencies.md) for detailed patterns including `yield` with `scope`, and class dependencies.

Use dependencies when the logic can't be declared in Pydantic validation, depends on external resources, needs cleanup (with `yield`), or is shared across endpoints.

Apply shared dependencies at the router level via `dependencies=[Depends(...)]`.

## Production patterns

See [`production-patterns.md`](references/production-patterns.md) for: lifespan (`@asynccontextmanager`, never `on_event`); graceful shutdown (uvicorn already handles SIGTERM, don't add your own); DI via `app.state` wrappers; middleware order (CORS → RequestID → Timing → Logging); `ContextVar` for request-scoped data; hiding `/docs` in prod.

Exception handlers → [`exception-handlers.md`](references/exception-handlers.md) — RFC 9457 Problem Details, `DomainError` hierarchy, `RequestValidationError` override, `RateLimitError` + `Retry-After`.

## Health checks

See [the health-checks reference](references/health-checks.md) — `/livez` and `/readyz` endpoints, healthy / degraded / unhealthy states, per-component reporting, and how the `app.state.startup_complete` / `shutting_down` flags wire in from the lifespan.

## Database

See [the database reference](references/database.md) — **SQLModel** preferred (Pydantic + SQLAlchemy 2.0), **PostgreSQL** for prod, **SQLite** OK for local tests. Covers `AsyncEngine` setup, the pool flags that matter (`pool_pre_ping`, `pool_recycle`), sizing formula, and when PgBouncer pays off.

## Caching

See [`cache.md`](references/cache.md) — `RedisDep` from lifespan, `cache_aside` helper for service methods, mutation→invalidation pattern, "don't use response-caching middleware" anti-pattern, lifespan cache-warming. Redis fundamentals (connection pools, TTL strategies, invalidation patterns) live in `python-infrastructure`.

## Kubernetes

See [the kubernetes reference](references/kubernetes.md) for the deployment side: shutdown sequence diagram, full Deployment YAML (probes / resources / lifecycle / preStop), PodDisruptionBudget, HPA on RPS vs CPU, `terminationGracePeriodSeconds` math, and the explicit "DO NOT install signal handlers" rule. Probe endpoints (`/livez`, `/readyz`) come from `production-patterns.md`.

## File handling (downloads, uploads, range requests)

See [the file-handling reference](references/file-handling.md) for `FileResponse` (downloads from disk with path-traversal guard), `StreamingResponse` for generated content (CSV, ZIP), HTTP-range requests for video / resumable downloads, `UploadFile` patterns with chunked reads, and two temp-file cleanup approaches (generator-with-`finally` vs `BackgroundTasks`).

## Observability

See [the observability reference](references/observability.md) for FastAPI-specific OTel usage: when auto-instrumentation is enough, `FastAPIInstrumentor.instrument_app(...)`, `excluded_urls` for `/livez` / `/readyz` / `/metrics`, `server_request_hook` for per-request attributes, and the rule for when to wrap a business operation in a manual span vs let the framework do it. SDK setup, samplers, semconv, Collector pipelines all live in the **`otel`** skill — this reference doesn't duplicate them.

## Anti-patterns

See [the anti-patterns reference](references/anti-patterns.md) for the quick-lookup table of common mistakes — blocking I/O in `async def`, `python-jose`, deprecated `json_encoders`, contradictory `Field(ge=..., default=None)`, `ORJSONResponse`, long-lived `httpx.AsyncClient` per request, mocking the DB in integration tests, and ~25 more. Scan it when reviewing a diff.

## Authentication & Authorization

Split across two references — pick the one that matches the question.

[`authn.md`](references/authn.md) — **who is the request?**

- Password hashing with `pwdlib` (Argon2 + bcrypt fallback).
- Self-issued JWT with `PyJWT`.
- External OIDC token verification (rolled in-house, no wrapper lib) — provider quick-start for Keycloak / Dex / Okta / Auth0 / Entra / Google + local-dev IdP.
- Protected-routes patterns on endpoints.

[`authz.md`](references/authz.md) — **what may they do?**

- Coarse role / scope checks (`require_active`, `require_superuser`).
- Fine-grained relational permissions with **OpenFGA** — model, `check` / `batch_check` / `list_objects` / `list_users`, writing tuples after DB mutations, service-layer integration with post-filtering, local Playground.

Rule of thumb: smallest mechanism that fits. Local JWT / OIDC → coarse deps → OpenFGA on top, never the reverse.

## Async vs Sync _path operations_

Use `async` _path operations_ only when fully certain that the logic called inside is compatible with async and await (it's called with `await`) or that doesn't block.

```python
from fastapi import FastAPI

app = FastAPI()


# Use async def when calling async code
@app.get("/async-items/")
async def read_async_items():
    data = await some_async_library.fetch_items()
    return data


# Use plain def when calling blocking/sync code or when in doubt
@app.get("/items/")
def read_items():
    data = some_blocking_library.fetch_items()
    return data
```

In case of doubt, or by default, use regular `def` functions, those will be run in a threadpool so they don't block the event loop.

The same rules apply to dependencies.

Make sure blocking code is not run inside of `async` functions. The logic will work, but will damage the performance heavily.

When needing to mix blocking and async code, see the Asyncer pattern in [`production-patterns.md`](references/production-patterns.md) § Bridging sync ↔ async.

## Constrained query values + pagination

For fixed-set query values (sort order, status, format) use `StrEnum` — auto-documents as a dropdown in `/docs` and beats `Query(pattern="^(asc|desc)$")`:

```python
from enum import StrEnum
class SortOrder(StrEnum):
    asc = "asc"
    desc = "desc"
```

For list endpoints, see [the pagination reference](references/pagination.md) — offset / cursor / keyset strategies, reusable `PaginationParams` dep, generic `Page[Item]` envelope (PEP 695), combined filter+sort+pagination dep, index requirements, approximate counts, `MAX_OFFSET` guards.

## Streaming (JSON Lines, SSE, bytes)

See [the streaming reference](references/streaming.md) for JSON Lines, Server-Sent Events (`EventSourceResponse`, `ServerSentEvent`), and byte streaming (`StreamingResponse`) patterns.

## WebSockets & rate limiting

[`websockets.md`](references/websockets.md) — authn BEFORE `accept()`, `ConnectionManager` on `app.state`, server-side heartbeat, NATS JetStream for horizontal scaling, manual OTel spans; prefer SSE for one-way push. [`rate-limiting.md`](references/rate-limiting.md) — `slowapi` per-route (not global middleware), Redis-backed via `app.state.redis`, key by `user_id` not IP; mandatory on `/login`, `/token`, `/forgot-password`.

## Tooling & libraries

Single source of truth in sibling skills + linked references: **`uv` / `ruff` / `ty`** (writing-python + astral:*), **`SQLModel`** over SQLAlchemy ([`database.md`](references/database.md)), **`HTTPX`** over requests (lifespan + `HttpDep`), **`Asyncer`** for sync↔async ([`production-patterns.md`](references/production-patterns.md)), **`PyJWT`** over python-jose, **`pwdlib`** over passlib ([`authn.md`](references/authn.md)).

## Microservices

See [`microservices.md`](references/microservices.md) — when to split, one-DB-per-service, sync (`httpx` ≤2 hops) vs async (NATS JetStream), outbox pattern, trace context, service-to-service authn, contract sharing, anti-patterns. **No Dapr, no Kafka.**

## Do not use Pydantic RootModels

Do not use Pydantic `RootModel`, instead use regular type annotations with `Annotated` and Pydantic validation utilities.

For example, for a list with validations you could do:

```python
from typing import Annotated

from fastapi import Body, FastAPI
from pydantic import Field

app = FastAPI()


@app.post("/items/")
async def create_items(items: Annotated[list[int], Field(min_length=1), Body()]):
    return items
```

instead of:

```python
# DO NOT DO THIS
from typing import Annotated

from fastapi import FastAPI
from pydantic import Field, RootModel

app = FastAPI()


class ItemList(RootModel[Annotated[list[int], Field(min_length=1)]]):
    pass


@app.post("/items/")
async def create_items(items: ItemList):
    return items

```

FastAPI supports these type annotations and will create a Pydantic `TypeAdapter` for them, so that types can work as normally and there's no need for the custom logic and types in RootModels.

## Use one HTTP operation per function

Don't mix HTTP operations in a single function, having one function per HTTP operation helps separate concerns and organize the code.

Do this:

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str


@app.get("/items/")
async def list_items():
    return []


@app.post("/items/")
async def create_item(item: Item):
    return item
```

instead of this:

```python
# DO NOT DO THIS
from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str


@app.api_route("/items/", methods=["GET", "POST"])
async def handle_items(request: Request):
    if request.method == "GET":
        return []
```
