# Exception Handlers (RFC 9457 Problem Details)

One global handler per domain exception class. Routes raise domain exceptions, never construct `HTTPException` directly (auth deps are the only exception, since 401/403 mapping is part of the OAuth2 / OIDC spec).

## Contents

- Domain exception hierarchy
- Global handler (RFC 9457 Problem Details)
- `RequestValidationError` override
- `RateLimitError` with `Retry-After`
- Rules

## Domain exception hierarchy

```python
# core/exceptions.py
class DomainError(Exception):
    status_code = 500
    title = "Internal Server Error"


class NotFoundError(DomainError):
    status_code = 404
    title = "Not Found"


class ConflictError(DomainError):
    status_code = 409
    title = "Conflict"


class AuthorizationError(DomainError):
    status_code = 403
    title = "Forbidden"


class ExternalServiceError(DomainError):
    status_code = 502
    title = "Bad Gateway"
```

## Global handler

Map every `DomainError` subclass to `application/problem+json` once:

```python
# main.py
from fastapi import Request
from fastapi.responses import JSONResponse


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"https://api.example.com/problems/{exc.__class__.__name__.lower()}",
            "title": exc.title,
            "status": exc.status_code,
            "detail": str(exc) or exc.title,
        },
        media_type="application/problem+json",
    )
```

Sensitive info (stack traces, SQL, file paths) never reaches the response — the handler returns a stable `title` + `detail`, and logging carries the trace via `log.exception(...)` inside the active OTel span (see [`observability.md`](observability.md)).

## `RequestValidationError` override

The default FastAPI 422 body is verbose and inconsistent with your Problem Details shape. Reformat it:

```python
from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "type": "https://api.example.com/problems/validation",
            "title": "Validation Error",
            "status": 422,
            "errors": [
                {
                    "field": ".".join(str(p) for p in e["loc"]),
                    "message": e["msg"],
                    "type": e["type"],
                }
                for e in exc.errors()
            ],
        },
        media_type="application/problem+json",
    )
```

## `RateLimitError` with `Retry-After`

When a rate-limited handler raises `RateLimitError(retry_after=60)`, the response must include the `Retry-After` header (HTTP spec):

```python
class RateLimitError(DomainError):
    status_code = 429
    title = "Too Many Requests"

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after


@app.exception_handler(RateLimitError)
async def rate_limit_handler(request: Request, exc: RateLimitError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "type": "https://api.example.com/problems/rate-limit",
            "title": exc.title,
            "status": 429,
            "retry_after": exc.retry_after,
        },
        headers={"Retry-After": str(exc.retry_after)},
        media_type="application/problem+json",
    )
```

## Rules

- **One handler per exception class**, registered in `main.py`. Don't scatter try/except in routes.
- **Routes raise domain exceptions**, never `HTTPException` (auth deps excepted — see `authn.md`).
- **Never include exception internals** in the response body — those leak via logs only.
- **`media_type="application/problem+json"`** on every Problem-shaped response.
- **Logging happens inside the handler**, not in the route — `log.exception(...)` inside the active span attaches `trace_id`/`span_id` automatically when OTel is active.
- **No middleware-based error handlers.** Use `@app.exception_handler(...)` — middleware is the wrong layer (see `anti-patterns.md`).
