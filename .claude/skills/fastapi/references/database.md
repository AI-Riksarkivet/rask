# Database (SQLModel + asyncpg + Connection Pooling)

How databases plug into FastAPI in this project: engine built once in lifespan, sessions per request via DI, sizing the pool, and the few knobs that actually matter in production.

## Contents

- Stack — SQLModel + SQLAlchemy 2.0 async + asyncpg
- Engine creation (lifespan)
- Pool configuration — the flags that matter
- Sizing — formula + worked example
- Settings — one place, one source
- Sessions are per-request — engine is per-process
- PgBouncer — when to add it
- Anti-patterns

## Stack

| Layer            | Choice                                                              |
| ---------------- | ------------------------------------------------------------------- |
| ORM / models     | **SQLModel** (Pydantic + SQLAlchemy 2.0) — preferred over bare SQLAlchemy |
| Database (prod)  | **PostgreSQL** — preferred default                                  |
| Database (local) | **SQLite** (via `aiosqlite`) is fine for local tests and small CLIs |
| Async driver     | **asyncpg** for Postgres, **aiosqlite** for SQLite                  |
| Engine API       | `sqlalchemy.ext.asyncio.AsyncEngine` (SQLAlchemy 2.0 native async)  |
| Migrations       | Alembic (async template)                                            |
| Settings         | `pydantic-settings` `BaseSettings` with `DB_` env prefix            |

> One engine per process, one session per request. The engine builds its own pool — **do not** call `asyncpg.create_pool` directly.

**SQLite vs Postgres.** SQLite is a great fit for unit tests, single-user CLIs, and prototypes (`sqlite+aiosqlite:///:memory:`). For anything multi-process, multi-writer, or production, use Postgres. SQLModel works identically against both; only the URL changes.

## Engine creation (lifespan)

```python
# core/db.py
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings


def make_engine() -> AsyncEngine:
    return create_async_engine(
        str(settings.DATABASE_URL),                # postgresql+asyncpg://...
        pool_size=settings.DB_POOL_SIZE,           # warm connections
        max_overflow=settings.DB_MAX_OVERFLOW,     # burst capacity above pool_size
        pool_pre_ping=True,                        # kill stale connections at checkout
        pool_recycle=1800,                         # rotate every 30 min — avoids PG idle timeouts
        pool_timeout=30,                           # waiting longer than this is a 503 signal
        connect_args={"command_timeout": 60},      # asyncpg per-query timeout
    )
```

Build it in lifespan, stash on `app.state.db_engine`, dispose after `yield`. See `production-patterns.md` § Lifespan.

## Pool configuration — the flags that matter

| Flag                     | Why                                                                                                                |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `pool_pre_ping=True`     | **The single most important flag**. Issues a cheap round-trip at checkout and discards dead sockets (NAT timeout, DB restart, network blip). Cost: one `SELECT 1` per checkout. Without it: intermittent `OperationalError` storms after any DB hiccup. |
| `pool_recycle=1800`      | Rotate connections every 30 min. Avoids PostgreSQL `idle_in_transaction_session_timeout` closing the socket under you. |
| `pool_timeout=30`        | How long a request waits for a connection. Longer = silent latency; shorter = explicit 503 → useful alert signal.  |
| `pool_size`              | Warm, always-open connections.                                                                                     |
| `max_overflow`           | Extra connections allowed above `pool_size` during bursts. Returned to OS after the burst.                         |
| `connect_args={"command_timeout": 60}` | asyncpg per-query timeout. Stops a single slow query starving the pool.                                            |

## Sizing — formula + worked example

```
pool_size = (concurrent_in_flight_db_queries * avg_query_duration_s) / target_latency_budget_s
max_overflow ≈ pool_size           # so the pool can roughly double under spike
```

Worked example for the viewer at 100 concurrent requests, 10 ms avg query, 50 ms latency budget:

```
pool_size    = (100 * 0.010) / 0.050 = 20
max_overflow = 20                    → up to 40 connections per replica under burst
```

Multiply by **replica count** to size the Postgres `max_connections` allowance. With 3 replicas at `(20 + 20)`, you need at least 120 connections on the DB side.

## Settings — one place, one source

```python
# core/config.py — DB-scoped BaseSettings
from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class DbConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_", env_file=".env", extra="ignore")
    DATABASE_URL: PostgresDsn
    POOL_SIZE: int = Field(default=10, ge=1, le=100)
    MAX_OVERFLOW: int = Field(default=20, ge=0, le=200)
```

## Sessions are per-request — engine is per-process

The `SessionDep` wrapper (see `production-patterns.md` § DI from `app.state`) yields one `AsyncSession` per request and commits/rollbacks around the handler. Never create a module-level session — sessions hold a checked-out connection until commit/rollback, so a long-lived one starves the pool.

```python
# api/deps.py — already covered in production-patterns.md, repeated for completeness
from collections.abc import AsyncGenerator
from typing import Annotated
from fastapi import Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(request.app.state.db_engine, expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_db)]
```

Routes consume `SessionDep` — never read `app.state.db_engine` directly.

## PgBouncer — when to add it

If your `(replicas × (pool_size + max_overflow))` exceeds the Postgres server's `max_connections`, put **PgBouncer** in front (transaction-mode). It multiplexes app connections onto a smaller server-side pool, so the database sees ~20–50 connections while your apps think they have hundreds.

Caveats for PgBouncer transaction-mode:

- No prepared statements (asyncpg uses them by default — set `statement_cache_size=0` or use PgBouncer in session-mode for asyncpg-friendliness).
- No `LISTEN` / `NOTIFY` (use NATS instead).
- No advisory locks across statements within a session.

For the rask viewer's current load (single replica, < 50 in-flight), PgBouncer is over-engineering. Add it when you cross 80% of `max_connections`.

## Anti-patterns

- `asyncpg.create_pool(...)` while ALSO using `AsyncEngine` — two pools fighting for the same DB connection cap.
- Module-level `engine = create_async_engine(...)` — connects at import, breaks tests, can't dispose. Always lifespan.
- `NullPool` in async code "for safety" — every request opens + closes a TCP connection. ~50× slower than pooling.
- `pool_recycle=-1` (no recycle) — PostgreSQL `idle_in_transaction_session_timeout` will eventually close the socket; you'll see `OperationalError: server closed the connection unexpectedly`.
- Pool sized to "the max" (`pool_size=100, max_overflow=200`) — DB hits its connection cap, the next replica boot fails. Size for the **service**, not the database.
- Manual `await conn.close()` inside a route after `async with` — already done by the context manager; double-closing leaks pool slots.
- Importing SQLAlchemy models from non-SQLModel `sqlalchemy.orm.DeclarativeBase` — use `SQLModel` so the same class can validate Pydantic input AND map to the DB row.
