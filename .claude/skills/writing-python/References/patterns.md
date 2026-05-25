# Everyday Patterns

Reference for project layout, type idioms, async, context managers, pathlib, and logging.

## Contents

- Project structure
- Type hints
- Pydantic models (instead of @dataclass)
- Custom exceptions
- Async patterns
- Context managers
- Data validation with Pydantic
- Pathlib
- Logging
- Style guidelines

## Project structure

```
src/
└── mypackage/
    ├── __init__.py
    ├── __main__.py      # CLI entry
    ├── domain/          # Business logic
    ├── services/        # Operations
    └── adapters/        # External integrations
tests/
pyproject.toml
```

## Type hints

```python
def get_user(user_id: str) -> User | None: ...

def process_items(items: Iterable[Item], *, limit: int = 100) -> list[Result]: ...

async def fetch(url: str, timeout: float = 30.0) -> bytes: ...
```

See `type-safety.md` for generics, protocols, narrowing.

## Pydantic models (instead of @dataclass)

```python
from pydantic import BaseModel, Field

class Config(BaseModel):
    host: str
    port: int = Field(default=8080, ge=1, le=65535)
    tags: list[str] = Field(default_factory=list)

# Construction validates
cfg = Config(host="0.0.0.0", port=9000)

# Serialize / deserialize
cfg.model_dump()           # -> dict
cfg.model_dump_json()      # -> str
Config.model_validate(payload)         # from dict
Config.model_validate_json(raw_json)   # from JSON string
```

For settings, use `pydantic-settings` — see `configuration.md`.

## Custom exceptions

```python
class AppError(Exception):
    """Base for application errors."""

class NotFoundError(AppError):
    def __init__(self, resource: str, id: str) -> None:
        self.resource = resource
        self.id = id
        super().__init__(f"{resource} not found: {id}")

class ValidationError(AppError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


def get_user(user_id: str) -> User:
    user = db.get(user_id)
    if user is None:
        raise NotFoundError("User", user_id)
    return user
```

See `error-handling.md` for full exception strategies.

## Async patterns

```python
import asyncio
import httpx

async def fetch_all(urls: list[str]) -> list[bytes]:
    async with httpx.AsyncClient() as client:
        tasks = [client.get(url) for url in urls]
        responses = await asyncio.gather(*tasks)
        return [r.content for r in responses]

async def fetch_with_timeout(url: str, timeout: float = 30.0) -> bytes:
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            return response.content
```

For structured concurrency, use `asyncio.TaskGroup`:

```python
async def fetch_both(a_url: str, b_url: str) -> tuple[bytes, bytes]:
    async with asyncio.TaskGroup() as tg:
        a_task = tg.create_task(fetch(a_url))
        b_task = tg.create_task(fetch(b_url))
    return a_task.result(), b_task.result()
```

## Context managers

```python
from contextlib import contextmanager, asynccontextmanager

@contextmanager
def open_db_connection(url: str):
    conn = create_connection(url)
    try:
        yield conn
    finally:
        conn.close()

@asynccontextmanager
async def get_session():
    session = await create_session()
    try:
        yield session
    finally:
        await session.close()
```

See `resource-management.md` for class-based managers, `ExitStack`, streaming.

## Data validation with Pydantic

```python
from pydantic import BaseModel, EmailStr, field_validator

class CreateUserRequest(BaseModel):
    name: str
    email: EmailStr

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be empty")
        return v.strip()
```

## Pathlib

```python
from pathlib import Path
import json

def process_files(directory: Path) -> list[Path]:
    return list(directory.glob("**/*.json"))

def read_config(path: Path) -> dict:
    return json.loads(path.read_text())
```

Prefer `Path` over `os.path` everywhere.

## Logging

This project routes logging via OpenTelemetry. Configure the stdlib `logging` once; OTel auto-instrumentation forwards records to the OTLP exporter.

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)
log.info("processing_started", extra={"count": len(items)})
```

For tracing, metrics, and full OTel setup, see the `python-infrastructure` skill (`References/observability.md`) and the `otel` skill.

## Style guidelines

- `snake_case` for functions and variables.
- `PascalCase` for classes.
- `SCREAMING_SNAKE_CASE` for module-level constants.
- Prefer `pathlib.Path` over `os.path`.
- f-strings for formatting.
- Context managers for resources.
- Avoid mutable default arguments (`def f(x: list | None = None):`, create inside).
- Pydantic, not `@dataclass`.
