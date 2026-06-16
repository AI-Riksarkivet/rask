# Dissolve `viewer` into a shared `core` brick (strangler cycle 4 — capstone)

**Date:** 2026-06-16
**Status:** Approved (design), pending implementation plan
**Strategy:** End of the strangler. The three independent services (volumes-api, search-api,
ray-api) are already severed. `core-api` and `orchestrator` are transactionally coupled (shared
`batches` table, `Batch` model, `batch_repo`, `submit_chunk`, `reconcile_from_s3`, alembic) and
cannot be independent — so `viewer` (which still holds all that shared code) is renamed and
absorbed into a single shared **`core`** brick that both entrypoints compose. This unifies code
ownership without collapsing the two processes (that topology decision is deferred to the Helm
deployment cycle).

## Problem

After cycles 1–3, only `core-api` and `orchestrator` still import `viewer`. `viewer` still owns
the entire DB/domain layer (models, repositories, db engine, alembic, batches/submission/sync,
the orchestrator loop+derive, the catalog discover service, all the batches/chunks/catalog/
orchestrator endpoints, and the monolith `main.py`). The `viewer` name is now a misnomer (it is
the core domain, not a viewer), and the two facades reach into its internals. The strangler ends
by dissolving `viewer` into a properly named `core` brick.

## Decision (from brainstorming)

- **Dissolve to a shared `core` brick; keep two entrypoints.** Move all of viewer's real code
  into `components/services/core/`; `core-api` + `orchestrator` stay as thin entrypoints that
  import from `core` (repointed `viewer.* → core.*`). Delete the `viewer` brick + `projects/viewer`.
- **Do NOT collapse the two processes** this cycle. The single-loop / scalable-API topology and
  the fleet (`dev-micro.sh`, gateway) stay exactly as they are. Whether to run core-api +
  orchestrator as one process is a deployment-topology choice deferred to the Helm cycle.
- **Flatten + drop the cycle-1 shims.** `viewer/core/{config,exceptions,middleware}.py` were
  transitional re-export shims to `service_kit`; they are dropped. `core` code imports
  `service_kit.{config,exceptions,middleware}` directly. `viewer/core/db.py` and
  `viewer/core/lifespan.py` flatten to `core/db.py` and `core/lifespan.py`.
- **Keep `core/main.py`** (the monolith app factory) — the moved test suite exercises it, and it
  remains a valid single-process entry.

## Design

### 1. New `components/services/core/` brick (absorbs viewer)

```
components/services/core/
  alembic/                         # ← viewer/alembic/ (env.py, versions/, script.py.mako)
  alembic.ini                      # ← viewer/alembic.ini
  pyproject.toml                   # name "core" (see §3)
  src/core/
    __init__.py                    # ← viewer/__init__.py
    main.py                        # ← viewer/main.py (create_app + module-level app)
    db.py                          # ← viewer/core/db.py        (make_engine, make_sessionmaker)
    lifespan.py                    # ← viewer/core/lifespan.py  (make_lifespan + orchestrator-task fns)
    models/{batch,enums,pipelines}.py
    repositories/batch.py
    schemas/{batch,catalog,chunk,health,orchestrator,sync}.py
    services/{batches,submission,sync}.py
    services/orchestrator/{__init__,derive,loop}.py
    services/discover/catalog.py
    api/dependencies.py
    api/v1/router.py
    api/v1/endpoints/{health,batches,chunks,catalog,orchestrator,spa}.py
  tests/                           # ← viewer/tests/ (conftest + the monolith-app suite)
```

The internal directory structure is preserved EXCEPT `viewer/core/` is flattened: `db.py` and
`lifespan.py` move up to `src/core/`, and the three shim files are deleted.

### 2. Import repoint rules (applied to all moved code + both entrypoints + tests)

| From | To |
|---|---|
| `viewer.core.config` | `service_kit.config` |
| `viewer.core.exceptions` | `service_kit.exceptions` |
| `viewer.core.middleware` | `service_kit.middleware` |
| `viewer.core.db` | `core.db` |
| `viewer.core.lifespan` | `core.lifespan` |
| `viewer.<anything-else>` (models, repositories, schemas, services, api, main) | `core.<same>` |

After the move, `grep -rn "viewer" components/ packages/ projects/ --include=*.py` returns
nothing (no `viewer.` import, no `viewer` package reference). The literal string `viewer` may
legitimately survive ONLY in: domain content unrelated to the package (e.g. the `RASK_VIEWER_INPUT`
/ `viewer_input` Settings field names, which are env-var/config names owned by `service_kit`, NOT
the `viewer` package — those stay), and historical doc/spec files under `docs/`.

### 3. `core` brick `pyproject.toml`

Name `core`. Dependencies are everything the absorbed code imports directly (verified from the
inventory): `service-kit`, `storage`, `ray-kit`, plus the heavy libs `sqlmodel`, `sqlalchemy`,
`lancedb`, `alembic`, `httpx`, `ray`, `pydantic`, `anyio`, `asyncpg`/`aiosqlite` (whatever
viewer currently declares for the DB drivers). The implementation plan copies viewer's current
dependency set verbatim and renames the package — viewer's pyproject is the source of truth for
the exact pins. `[tool.uv.sources]` carries the workspace entries (`service-kit`, `storage`,
`ray-kit`). Wheel packages `["src/core"]`. Alembic packaging: ensure the `alembic/` dir ships /
is runnable via `uv run --package core alembic ...` (mirror how viewer's pyproject exposed it).

### 4. The two entrypoints (thin shells over `core`, behaviour unchanged)

`components/services/core_api/src/core_api/__init__.py`:
```python
from core.api.v1.endpoints import batches, catalog, chunks, health
from core.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(
    title="core-api",
    routers=[health.router, batches.router, chunks.router, catalog.router],
    lifespan=make_lifespan,
)
```

`components/services/orchestrator/src/orchestrator/__init__.py`:
```python
from core.api.v1.endpoints import health, orchestrator
from core.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(title="orchestrator", routers=[health.router, orchestrator.router], lifespan=make_lifespan)
```

`core_api`/`orchestrator` brick `pyproject.toml`: drop `viewer` from dependencies + sources, add
`core`. `projects/core-api/` and `projects/orchestrator/`: `[tool.uv.workspace] members` replace
`../../components/services/viewer` with `../../components/services/core`; `[tool.uv.sources]`
drop `viewer`, add `core`.

### 5. Deletions

- `components/services/viewer/` (the entire brick: src, alembic, tests, pyproject).
- `projects/viewer/` (the `viewer-project` deployable — no longer built; the fleet never ran it).

### 6. Workspace + tooling bookkeeping

- Root `pyproject.toml` `[tool.uv.workspace] members`: replace `components/services/viewer` with
  `components/services/core`.
- Root `pyproject.toml` `[tool.pytest.ini_options] testpaths`: replace
  `components/services/viewer/tests` with `components/services/core/tests`.
- Root `pyproject.toml` ruff `known-first-party`: replace `viewer` with `core` if listed.
- `Makefile`: `pg-migrate` / `pg-revision` (and any `alembic` invocation) repoint
  `uv run --package viewer alembic ...` → `uv run --package core alembic ...`.
- `.dagger/`: repoint the migrate-up / test steps that reference the `viewer` package or its
  alembic path to `core` (grep `.dagger` for `viewer`).
- Root `package.json` `workspaces`: `viewer` is Python-only and is NOT in `workspaces`, so no
  change there (verify).

### 7. Unchanged (zero disruption)

- `dev-micro.sh` — still launches `gateway`, `core_api`, `search_api`, `volumes_api`, `ray_api`,
  `orchestrator`. The module names (`core_api:app`, `orchestrator:app`) are unchanged.
- `gateway` — pure HTTP proxy; routes batches/chunks/catalog/health → core-api, orchestrator →
  orchestrator. No change.
- `service-kit`, `storage`, `ray-kit`, `htr`, the runner, the three independent services, the
  frontend — untouched.
- Helm chart's stale `viewer-deployment.yaml` is a known follow-up (the deployment cycle); not
  touched here (it is not deployed by the current fleet).

## Verification

- `uv sync --all-packages` resolves.
- `grep -rn "from viewer\|import viewer\|--package viewer\|services/viewer" components packages projects Makefile .dagger`
  → nothing (the package is gone; only `viewer_input`/`RASK_VIEWER_*` config names may remain,
  which are service_kit-owned env names, not the package).
- Import-smoke: `import core, core.main, core_api, orchestrator, gateway` all OK; `import viewer`
  now FAILS (module gone).
- Full `not slow` suite green at the same count as before the move (the viewer test suite, now
  `core/tests`, is the safety net — it must pass unchanged in count).
- `uvx ty check` adds no new diagnostics; `uvx ruff check` clean of new findings.
- Alembic: `RASK_BATCHES_DB=... uv run --package core alembic upgrade head` works (or the dagger
  `migrate-up` equivalent) — proves the migrations moved correctly.
- Live: restart the fleet (do NOT touch Ray; never `make ray-down`); `/api/batches/`,
  `/api/chunks/` (list), `/api/catalog/browse`, `/api/orchestrator/state` all 200 through the
  gateway; the orchestrator loop still ticks and submits HTR.

## Risks / mitigations

- **Large mechanical move (~30 source files + tests + alembic).** Mitigation: do it as one
  atomic task (viewer can't be half-moved) using `git mv` for the tree + scripted, rule-based
  import repoints, gated by the full grep + suite + import-smoke before commit. The two-stage
  review (spec + quality) plus a holistic final review catch stragglers.
- **Alembic path/packaging.** Mitigation: verify `uv run --package core alembic upgrade head`
  against an ephemeral SQLite/PG before claiming done; the Dagger `migrate-up` is the CI proof.
- **A missed `viewer.` import.** Mitigation: the grep gate is the hard stop — zero `viewer`
  package references anywhere is a done-criterion.

## Non-goals (this cycle)

- No process collapse (core-api + orchestrator stay two deployables).
- No Helm/k8s changes (the deployment cycle owns `viewer-deployment.yaml` → core, and the 1-vs-2
  process decision).
- No behaviour change to any endpoint or the orchestrator loop.
- No NATS work.

## Follow-up

The Helm per-service deployment cycle (Deployment+Service per service, gateway env-routing,
split images, and the core-api/orchestrator 1-vs-2-process topology decision) — the cycle that
turns all this code isolation into real separate pods. Then, eventually, the NATS JetStream
consumer that replaces the in-process orchestrator loop.
