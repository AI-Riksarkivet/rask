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
- Migrations (Alembic)
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

## Migrations (Alembic)

Schema changes go through Alembic. SQLModel + Alembic is two lines of glue in `env.py` and the standard CLI workflow afterwards.

### Init + `env.py` for SQLModel

Initialize with the **async template** so the generated `env.py` knows about `AsyncEngine`:

```bash
uv run alembic init -t async alembic
```

The only SQLModel-specific edits to `env.py`:

```python
# alembic/env.py
from sqlmodel import SQLModel
# IMPORTANT: import every module that defines a table model so SQLModel.metadata
# is populated before autogenerate runs. One blanket import is fine.
from app import models  # noqa: F401

target_metadata = SQLModel.metadata
```

And inside both `run_migrations_online()` and `run_migrations_offline()`, pass these flags to `context.configure(...)`:

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    compare_type=True,           # detect column-type changes (e.g. String -> Text)
    compare_server_default=True, # detect server_default changes
    render_as_batch=True,        # CRITICAL for SQLite — emits ALTER TABLE via copy-and-rename
)
```

`render_as_batch=True` matters because SQLite doesn't support most `ALTER TABLE` operations; Alembic's "batch mode" works around it by recreating the table. Leave it on even if your prod is Postgres — local SQLite tests run the same migrations.

### `script.py.mako` — add `import sqlmodel`

SQLModel's autogenerated migrations sometimes reference `sqlmodel.sql.sqltypes.AutoString` and similar. Add one line to the template so generated scripts have the import ready:

```mako
# alembic/script.py.mako — near the top, alongside `import sqlalchemy as sa`
import sqlmodel
```

If you still see `NameError: name 'sqlmodel' is not defined` in a generated migration, the autogenerated type is probably wrong — hand-edit it to the standard SQLAlchemy type (`sa.String`, `sa.Text`).

### Constraint naming convention — set this on day 1

Without an explicit naming convention, Alembic autogenerates anonymous constraints (`fk_xxx_abc123`) — rollbacks then fail because the name doesn't match. Define it once on `SQLModel.metadata` **before** any model imports:

```python
# core/db.py — at module top, before any SQLModel subclass is defined
from sqlmodel import SQLModel

SQLModel.metadata.naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Backfill names on existing tables in a one-off migration if you're adopting this late — otherwise rollbacks of pre-convention migrations will keep working but new ones won't be portable.

### Workflow

```bash
# 1. Change a model in app/models/*.py
# 2. Generate migration (compares model -> live DB)
uv run alembic revision --autogenerate -m "add user.last_login_at"

# 3. ALWAYS open the generated file and read it. Autogenerate misses:
#    enum value changes, server defaults, check constraints, custom types,
#    column comments. Hand-edit before committing.

# 4. Apply locally
uv run alembic upgrade head

# 5. Commit BOTH the model change AND the migration file in the same PR.
```

### Deploy: `initContainer`, never the app lifespan

On Kubernetes, `alembic upgrade head` runs in an **initContainer** before the app container starts — not inside the app's `lifespan`. Two reasons: (1) N replicas → N concurrent migration attempts → race; (2) a failed migration must crash the deploy, not flap the app pods.

```yaml
# deployment.yaml — excerpt
spec:
  template:
    spec:
      initContainers:
        - name: migrate
          image: registry.example.com/viewer:1.2.3   # same image as the app
          command: ["uv", "run", "alembic", "upgrade", "head"]
          envFrom: [{ secretRef: { name: viewer-db } }]
      containers:
        - name: api
          # ... see kubernetes.md
```

If the migration fails, the pod stays in `Init:Error`, the Deployment rollout stalls, and no new app pods come up with the broken schema. That's the correct behaviour.

For local dev (no k8s), `alembic upgrade head` runs from `make` or your prestart script. Never from `@app.on_event("startup")` or `lifespan` — that's an anti-pattern (see below).

### Data migrations — separate from schema

Schema migrations are "add column, change type, add index." Data migrations are "backfill this column for 50M existing rows." **Don't mix them in one revision** — schema migrations need to be fast (locks the table) and re-runnable; data migrations need to be batched and resumable.

```python
# alembic/versions/xxx_backfill_user_country.py — data migration in batches
def upgrade() -> None:
    conn = op.get_bind()
    batch = 5000
    while True:
        result = conn.execute(sa.text("""
            UPDATE "user" SET country = 'SE'
            WHERE country IS NULL AND id IN (
                SELECT id FROM "user" WHERE country IS NULL
                ORDER BY id LIMIT :n
            )
        """), {"n": batch})
        if result.rowcount == 0:
            break
```

For tables >100k rows or schemas under live traffic, prefer the **expand-migrate-contract** pattern: (1) add the new nullable column, (2) backfill in a separate migration (batched), (3) flip the app to read/write the new column, (4) drop the old column in a later release.

### Commands beyond `upgrade head`

| Command | When |
| ------- | ---- |
| `alembic current` | What revision is the DB on right now? |
| `alembic history` | Full revision tree (one line per migration) |
| `alembic upgrade head --sql` | Print the DDL without running it — for DBA review or pre-deploy diff |
| `alembic downgrade -1` | Roll back one revision (test downgrades locally before merging) |
| `alembic stamp head` | Mark DB as "up to date" without running migrations — for adopting Alembic on an existing DB whose schema already matches HEAD |
| `alembic heads` | List unmerged head revisions (will be >1 if two branches added migrations in parallel) |
| `alembic merge heads -m "merge X and Y"` | Create an empty merge revision joining the divergent heads back to a single line — required when two PRs both add migrations |

Merging branches is **mechanical**, not a conflict resolver — if the two branches added incompatible schema changes (e.g. both rename the same column), you must hand-edit one of the branch migrations before merging.

## Anti-patterns

- `asyncpg.create_pool(...)` while ALSO using `AsyncEngine` — two pools fighting for the same DB connection cap.
- Module-level `engine = create_async_engine(...)` — connects at import, breaks tests, can't dispose. Always lifespan.
- `NullPool` in async code "for safety" — every request opens + closes a TCP connection. ~50× slower than pooling.
- `pool_recycle=-1` (no recycle) — PostgreSQL `idle_in_transaction_session_timeout` will eventually close the socket; you'll see `OperationalError: server closed the connection unexpectedly`.
- Pool sized to "the max" (`pool_size=100, max_overflow=200`) — DB hits its connection cap, the next replica boot fails. Size for the **service**, not the database.
- Manual `await conn.close()` inside a route after `async with` — already done by the context manager; double-closing leaks pool slots.
- Importing SQLAlchemy models from non-SQLModel `sqlalchemy.orm.DeclarativeBase` — use `SQLModel` so the same class can validate Pydantic input AND map to the DB row.
- `SQLModel.metadata.create_all(engine)` in app startup — bypasses Alembic, drifts from prod schema, hides every migration bug. OK only for ephemeral test DBs created per test run.
- `alembic upgrade head` inside the app's `lifespan` — N replicas race the migration; one wins, the others either crash or see half-applied state. Use a k8s `initContainer` (see § Deploy).
- Schema + data changes in the same revision — schema migrations need to be fast and re-runnable; data backfills need to be batched and resumable. Split them.
- Committing a model change without the accompanying migration — the next person who runs `alembic upgrade head` won't get your column.
- Skipping `compare_type=True` / `render_as_batch=True` in `env.py` — autogenerate misses type changes (silent prod bugs) and breaks for any SQLite test DB.
- Editing an already-applied migration — Alembic's `alembic_version` table records the revision hash; if you edit the file, every environment re-running it diverges. Create a new revision instead.
