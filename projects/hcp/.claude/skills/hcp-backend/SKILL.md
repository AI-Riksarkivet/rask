---
name: hcp-backend
description: >
  HCP backend design patterns and mock server conventions.
  Use when: writing FastAPI endpoints, adding mock server routes,
  creating Pydantic schemas, or working with backend Python code.
---

# HCP Backend — Patterns & Conventions

## Stack

- **FastAPI** with async handlers
- **Pydantic v2** schemas for request/response models
- **Python 3.13+** — use modern syntax (`X | None`, `dict[str, Any]`, etc.)
- **uv** for dependency management
- **ruff** for linting/formatting, **ty** for type checking
- Quality: `make quality` runs ruff format/check + ty check

## Design Principles

### Composition over inheritance

Never subclass a service to add behavior. Wrap it:

```python
# GOOD — composition
class CachedMapiService:
    def __init__(self, inner: MapiService, cache: CacheService, ...):
        self._inner = inner

    async def request(self, method, path, **kwargs):
        # cache logic...
        return await self._inner.request(method, path, **kwargs)

# BAD — inheritance
class CachedMapiService(MapiService):  # tight coupling, brittle
    async def request(self, ...):
        return await super().request(...)
```

Use `inner` as the conventional name for the wrapped service.
Forward public methods explicitly; use `__getattr__` only for truly
generic delegation (e.g., LanceService has many passthrough methods).

### Clean layering — services never import from API

Dependency direction: `api/ → services/`, never the reverse.

- **Services** raise domain exceptions (`MapiError`, `StorageError`)
- **API layer** catches them and translates to `HTTPException`
- Services must never import `fastapi`, `HTTPException`, or `app.api.*`

```
app/api/errors.py     → catches domain exceptions → HTTPException
app/services/         → raises domain exceptions (MapiError, StorageError)
```

### Domain exceptions per subsystem

Each service domain defines its own exception hierarchy:

- `services/mapi/errors.py` → `MapiError`, `MapiTransportError`, `MapiResponseError`
- `services/storage/errors.py` → `StorageError`

The API layer provides helpers to translate these:
- `run_storage(coro, resource)` — catches `StorageError` → `HTTPException`
- `run_mapi(coro, resource)` — catches `MapiError` → `HTTPException`
- Global `@app.exception_handler(MapiError)` for endpoints that don't use `run_mapi`

### DRY — one path for error handling

Don't catch the same exception in multiple ways. If adapters convert
raw errors to domain exceptions internally, the API layer should only
catch domain exceptions — never raw library exceptions alongside them.

### Group by domain, not by pattern

Organize files by what they do, not what pattern they use:

```python
# GOOD — domain grouping
services/mapi/service.py    # MapiService
services/mapi/errors.py     # MapiError

# BAD — pattern grouping
services/mapi_service.py
services/mapi_errors.py
services/cached_mapi.py     # scattered across root
```

### Modern Python (3.13+)

- `X | None` not `Optional[X]`
- `dict[str, Any]` not `Dict[str, Any]`
- `from __future__ import annotations` in all modules
- Use `@property` for read-only attributes on composition wrappers

## Project Layout

```
backend/
├── app/
│   ├── api/
│   │   ├── errors.py              # run_storage, run_mapi, raise_for_hcp_status
│   │   ├── dependencies.py        # FastAPI DI overrides
│   │   └── v1/
│   │       ├── endpoints/mapi/    # MAPI API endpoints
│   │       │   ├── namespace/     # Namespace CRUD + sub-resources
│   │       │   ├── tenant/        # Tenant settings, users, groups
│   │       │   └── system/        # System-level admin
│   │       ├── endpoints/s3/      # S3-compatible API
│   │       ├── endpoints/query/   # Metadata query API
│   │       └── router.py          # Route registration
│   ├── schemas/                   # Pydantic models
│   │   ├── common.py              # Shared types (Role enum, etc.)
│   │   └── ...
│   ├── core/                      # Config, auth, security, telemetry
│   └── services/                  # Business logic — NO FastAPI imports
│       ├── cache/                 # Cache infrastructure + wrappers
│       │   ├── service.py         # CacheService (Redis)
│       │   ├── mapi.py            # CachedMapiService
│       │   ├── query.py           # CachedQueryService
│       │   ├── lance.py           # CachedLanceService
│       │   └── storage.py         # CachedStorage
│       ├── mapi/                  # HCP Management API
│       │   ├── errors.py          # MapiError hierarchy
│       │   ├── service.py         # MapiService + AuthenticatedMapiService
│       │   └── query.py           # QueryService + AuthenticatedQueryService
│       ├── lance/                 # Lance vector DB
│       │   ├── service.py         # LanceService
│       │   └── serialize.py       # PyArrow value serializer
│       └── storage/               # S3 storage adapters
│           ├── errors.py          # StorageError
│           ├── protocol.py        # StorageProtocol (ABC)
│           ├── factory.py         # create_storage, create_cached_storage
│           └── adapters/          # HCP, generic boto3
├── mock_server/
│   ├── mapi_state.py              # Request dispatcher + in-memory state
│   └── fixtures.py                # Seed data for development
└── tests/
```

## Mock Server

The mock server (`mock_server/`) provides a fake HCP MAPI backend for
frontend development. It uses in-memory state, not a database.

### State structure

```python
state.tenants = { "tenant-name": { ... } }
state.namespaces = { "tenant": { "ns-name": { ... } } }
state.ns_settings = { ("tenant", "ns"): { "protocols": {...}, "compliance": {...}, ... } }
state.users = { "tenant": { "username": { ... } } }
state.groups = { "tenant": { "groupname": { ... } } }
```

### Adding a new mock route

1. Find the dispatcher method in `mapi_state.py` (e.g., `_handle_namespaces`)
2. Parse URL segments to determine the sub-resource
3. Return JSON from state, or modify state for POST/PUT/DELETE
4. Match the exact response shape that the real HCP MAPI returns

### Important rules

- **Enum values must match Pydantic schemas exactly**
  - Roles: `ADMINISTRATOR`, `SECURITY`, `MONITOR`, `COMPLIANCE`
  - NOT `ADMIN`, `admin`, etc.
- **Fixture data in `fixtures.py`** must use these exact values
- **Response format**: MAPI wraps responses in XML-like JSON structures
  (e.g., `{ "name": { "namespaceSettings": [...] } }`)

## Required Checklist for Every Backend Change

Any change to the backend **MUST** include all three:

1. **Backend endpoint** — the actual FastAPI route in `app/api/v1/endpoints/`
2. **Mock server support** — matching route in `mock_server/mapi_state.py` + seed data in `fixtures.py`
3. **Tests** — pytest tests in `backend/tests/` covering the new/changed endpoint

The mock server is what the frontend develops against. If a backend endpoint
exists but the mock doesn't handle it, the frontend cannot be tested locally.
Never skip the mock or tests — they are not optional.

## Endpoint Pattern

```python
@router.get("/tenants/{tenant_name}/namespaces/{ns_name}")
async def get_namespace(
    tenant_name: str,
    ns_name: str,
    verbose: bool = False,
    request: Request = None,
):
    url = f"{base_url}/mapi/tenants/{tenant_name}/namespaces/{ns_name}"
    params = {"verbose": str(verbose).lower()} if verbose else {}
    response = await client.get(url, params=params)
    return handle_response(response)
```

## Required Skills

When working on backend code, **always activate these skills**:

- `astral:ruff` — Python linting and formatting
- `astral:uv` — Python package management
- `testing-python` — pytest patterns and best practices
- `fastapi-templates` — Best practices for fastapi patterns


## Testing

Tests live in `backend/tests/` and use pytest with async support.

```python
@pytest.mark.asyncio
async def test_export_namespace(client):
    response = await client.get("/api/v1/mapi/tenants/test/namespaces/ns1/export")
    assert response.status_code == 200
    data = response.json()
    assert "name" not in data  # read-only fields stripped
```
