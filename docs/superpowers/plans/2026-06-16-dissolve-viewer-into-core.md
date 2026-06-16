# Dissolve viewer into a shared `core` brick — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename/absorb the `viewer` brick into a new `core` brick (flattened, shims dropped), repoint the `core-api` + `orchestrator` entrypoints from `viewer.*` to `core.*`, and delete `viewer`. Behaviour-preserving; the two processes and the fleet stay unchanged.

**Architecture:** End of the strangler. `core` owns the DB/models/alembic/domain/endpoints/lifespan/monolith. `core-api` and `orchestrator` remain thin entrypoints over `core`. No process collapse, no Helm changes this cycle.

**Tech Stack:** Python 3.13, uv workspace, hatchling, FastAPI, SQLModel/SQLAlchemy async, Alembic, LanceDB, Ray (via ray-kit), `ty`, pytest (importlib mode).

---

## Notes for the implementer

- Run from repo root `/home/morgan/rask`. Branch is already `refactor/dissolve-viewer-into-core`.
- **Ray/uv gotcha:** any `uv run` importing `core`/`ray` needs `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and `--no-sync`. App imports need `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` set (these are service-kit Settings env names — they KEEP the `VIEWER` spelling; do NOT rename them).
- **Never run pytest with `-o addopts=""`** (drops `--import-mode=importlib`). Quiet runs: `... pytest -m "not slow" -p no:cacheprovider --no-header -q`.
- Commit rules: no `Co-Authored-By`, no Claude/AI mention, exact message, do not push.
- Behaviour-preserving. Baseline: full `not slow` suite is green on this branch — **111 passed** (verify once: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q`).
- **Do NOT stage unrelated files.** `.claude/skills/**` may show modified — never `git add -A`; stage explicit paths (this task legitimately touches many files, so stage the specific top-level dirs listed in the commit step, NOT `-A`).
- **CRITICAL distinction:** the PACKAGE `viewer` must vanish, but the strings `RASK_VIEWER_INPUT` / `RASK_VIEWER_OUTPUT` / `viewer_input` / `viewer_output` are service-kit config/env names and MUST survive. The repoint rules below target `viewer.` (dot) and `import viewer` / `from viewer`, which never match `viewer_input` or `RASK_VIEWER_*`.

---

## Task 1: Move viewer → `core`, flatten, repoint everything, delete viewer

This is one atomic task: `viewer` cannot be half-dissolved. Use `git mv` for the tree, then rule-based seds for the import repoints, then verify hard before committing.

**Files:** the entire `components/services/viewer/` brick (→ `core`), `core_api` + `orchestrator` (`src/__init__.py` + `pyproject.toml`), `projects/{core-api,orchestrator,viewer}`, root `pyproject.toml`, `Makefile`, `.dagger/{test,migrate}.go`.

- [ ] **Step 1: Baseline green**

```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
```
Expect `111 passed`. If not, STOP and report.

- [ ] **Step 2: Move + rename the brick, flatten `core/`, drop the shims**

```bash
git mv components/services/viewer components/services/core
git mv components/services/core/src/viewer components/services/core/src/core
# flatten the inner core/ subdir: db + lifespan move up; config/exceptions/middleware shims die
git mv components/services/core/src/core/core/db.py       components/services/core/src/core/db.py
git mv components/services/core/src/core/core/lifespan.py components/services/core/src/core/lifespan.py
git rm components/services/core/src/core/core/config.py \
       components/services/core/src/core/core/exceptions.py \
       components/services/core/src/core/core/middleware.py
git rm components/services/core/src/core/core/__init__.py
rmdir components/services/core/src/core/core 2>/dev/null || true
```
After this, confirm `components/services/core/src/core/core/` no longer exists and `core/db.py` + `core/lifespan.py` are present.

- [ ] **Step 3: Repoint imports across the moved code + the two entrypoints + tests**

Apply these seds (ORDER MATTERS — specific `viewer.core.*` rules first, then the general `viewer.` rule) to every `.py` under `components/services/core/src`, `components/services/core/tests`, `components/services/core/alembic`, `components/services/core_api/src`, `components/services/orchestrator/src`:

```bash
FILES=$(find components/services/core/src components/services/core/tests components/services/core/alembic \
              components/services/core_api/src components/services/orchestrator/src -name '*.py')
for f in $FILES; do
  sed -i \
    -e 's/viewer\.core\.config/service_kit.config/g' \
    -e 's/viewer\.core\.exceptions/service_kit.exceptions/g' \
    -e 's/viewer\.core\.middleware/service_kit.middleware/g' \
    -e 's/viewer\.core\.db/core.db/g' \
    -e 's/viewer\.core\.lifespan/core.lifespan/g' \
    -e 's/from viewer\./from core./g' \
    -e 's/import viewer\./import core./g' \
    -e 's/from viewer import/from core import/g' \
    "$f"
done
```
Then verify NO `viewer` package import remains anywhere:
```bash
grep -rn "from viewer\|import viewer" components/services/core components/services/core_api components/services/orchestrator && echo ">>> RESIDUAL viewer import (fix before continuing)" || echo "clean — no viewer imports in core/core_api/orchestrator"
```
If anything remains (e.g. a multi-line import, an odd alias), fix it by hand. NOTE the alembic `env.py` had `import viewer.models.batch` → must now read `import core.models.batch` (the sed handles it; confirm).

- [ ] **Step 4: Rewrite `components/services/core/pyproject.toml`**

```toml
[project]
name = "core"
version = "0.1.0"
description = "Core domain service — batches/chunks/catalog/orchestrator API, DB models, migrations, and the orchestrator loop."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "storage",
    "ray-kit",
    "fastapi>=0.115",
    "httpx>=0.27",
    "uvicorn>=0.30",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "lancedb>=0.20",
    "sqlmodel>=0.0.22",
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "ray[default]==2.55.1",
    "anyio>=4.0",
]

[project.optional-dependencies]
postgres = ["asyncpg>=0.30"]
migrations = ["alembic>=1.13"]

[project.scripts]
core = "core.main:main"

[dependency-groups]
dev = [
    "boto3-stubs[s3]>=1.34",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/core"]

[tool.uv.sources]
service-kit = { workspace = true }
storage = { workspace = true }
ray-kit = { workspace = true }
```
(This ADDS `service-kit` as a direct dep — viewer relied on workspace resolution for it; `core` declares it properly since it imports `service_kit` directly after the shims are gone.)

- [ ] **Step 5: Confirm the two entrypoints now read from `core`**

After Step 3's sed, `components/services/core_api/src/core_api/__init__.py` should be:
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
and `components/services/orchestrator/src/orchestrator/__init__.py`:
```python
from core.api.v1.endpoints import health, orchestrator
from core.lifespan import make_lifespan
from service_kit import make_service_app

app = make_service_app(title="orchestrator", routers=[health.router, orchestrator.router], lifespan=make_lifespan)
```
Verify the import ordering passes ruff (run `uvx ruff check --fix` on those two files if I001 complains).

- [ ] **Step 6: Repoint `core_api` + `orchestrator` brick manifests**

`components/services/core_api/pyproject.toml`: in `dependencies`, replace `"viewer"` with `"core"`; in `[tool.uv.sources]`, replace `viewer = { workspace = true }` with `core = { workspace = true }`. Keep `service-kit`.

`components/services/orchestrator/pyproject.toml`: same replacement (`viewer` → `core` in dependencies + sources).

- [ ] **Step 7: Repoint the project deployables; delete `projects/viewer`**

`projects/core-api/pyproject.toml`: in `[tool.uv.sources]` replace `viewer = { workspace = true }` with `core = { workspace = true }`; in `[tool.uv.workspace] members` replace `"../../components/services/viewer"` with `"../../components/services/core"`.

`projects/orchestrator/pyproject.toml`: same two replacements.

Delete the viewer deployable:
```bash
git rm -r projects/viewer
```

- [ ] **Step 8: Root workspace + pytest + ruff bookkeeping**

In root `pyproject.toml`:
- `[tool.uv.workspace] members`: replace `"components/services/viewer"` with `"components/services/core"`.
- `[tool.pytest.ini_options] testpaths`: replace `"components/services/viewer/tests"` with `"components/services/core/tests"`.
- ruff `known-first-party` (under `[tool.ruff.lint.isort]` or similar): if `"viewer"` is listed, replace with `"core"`. (grep the file for `known-first-party` and for `"viewer"`.)

- [ ] **Step 9: Repoint `Makefile` and `.dagger`**

`Makefile`: replace every occurrence of `viewer.main:app` → `core.main:app`, `--package viewer` → `--package core`, and `components/services/viewer` → `components/services/core`. (The `viewer` / `viewer-frontend` TARGET NAMES stay — only the commands they run change. Use a targeted `sed -i` on those three patterns, then eyeball the diff to be sure no target name or `RASK_VIEWER_*` got touched.)

`.dagger/test.go` and `.dagger/migrate.go`: replace `--package viewer` → `--package core`, `components/services/viewer` → `components/services/core` (the `WithWorkdir` path and the `pytest components/services/viewer/tests/` path). Leave prose comments mentioning "viewer suite" if you like, or update them — not load-bearing.

- [ ] **Step 10: Resolve, grep-gate, import-smoke, suite, alembic, types, lint**

```bash
uv sync --all-packages
echo "=== grep gate: no viewer PACKAGE refs anywhere (viewer_input / RASK_VIEWER_* are OK) ==="
grep -rnE "from viewer|import viewer|--package viewer|components/services/viewer|viewer\.main" components packages projects Makefile .dagger && echo ">>> RESIDUAL viewer reference (FAIL)" || echo "clean — no viewer package references"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import core, core.main, core_api, orchestrator, gateway; print('core + entrypoints import OK')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import importlib.util as u; print('viewer module gone:', u.find_spec('viewer') is None)"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
RASK_BATCHES_DB="sqlite+aiosqlite:///$(mktemp -u)/alembic_check.db" uv run --package core --extra migrations alembic -c components/services/core/alembic.ini upgrade head
uvx ty check packages components/services
uvx ruff check packages components/services
```
Expected: "clean — no viewer package references"; `core + entrypoints import OK`; `viewer module gone: True`; suite **111 passed** (same count — the viewer suite is now `core/tests`); the alembic `upgrade head` runs without error (creates the batches table in a throwaway sqlite — proves migrations moved correctly; if the exact alembic invocation differs from how the Makefile/dagger call it, use the Makefile's `pg-migrate` recipe form against a sqlite URL instead and report what worked); `ty` no new diagnostics beyond the known baseline; `ruff` clean of new findings (the 2 pre-existing E501s, now in `core/main.py` + `core/services/orchestrator/loop.py`, are out of scope). If anything fails, fix before committing. Do NOT commit on a red suite.

- [ ] **Step 11: Commit (explicit dirs, NOT `git add -A`)**

```bash
git add components/services/core components/services/core_api components/services/orchestrator \
        projects/core-api projects/orchestrator projects/viewer \
        pyproject.toml Makefile .dagger uv.lock
git commit -m "refactor(core): dissolve the viewer package into a shared core brick; core-api + orchestrator entrypoints repointed"
```
(The `projects/viewer` deletion is staged by naming the path even though the dir is gone — `git add projects/viewer` records the removal. Verify with `git status`.) After committing, run `git status --short` and confirm only `.claude/skills/**` remains unstaged and that `components/services/viewer` is fully gone from the tree.

---

## Task 2: Live verification through the gateway

**Files:** none (runtime check).

- [ ] **Step 1: Restart only the rask fleet (do NOT touch Ray; never `make ray-down`)**

```bash
pid=$(ss -tlnpH "sport = :8888" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
PGID=$(ps -o pgid= -p "${pid:-0}" 2>/dev/null | tr -d ' ')
[ -n "$PGID" ] && kill -TERM "-$PGID"
for i in $(seq 1 25); do busy=""; for p in 8888 8801 8802 8803 8804 8810; do ss -tlnH "sport = :$p" | grep -q . && busy="$busy $p"; done; [ -z "$busy" ] && break; read -t 1 <> <(:) || true; done
ORCH_AUTOSTART=true ./dev-micro.sh > /tmp/rask-fleet.log 2>&1 &
```
(Requires Ray + Postgres already up. `core_api:app` + `orchestrator:app` module names are unchanged, so `dev-micro.sh` needs no edits.)

- [ ] **Step 2: Hit the core-served routes + orchestrator through the gateway**

```bash
curl -s --retry 40 --retry-delay 1 --retry-connrefused --retry-all-errors -o /dev/null -w "gateway /api/batches/ -> %{http_code}\n" -L "http://127.0.0.1:8888/api/batches/"
curl -s -o /dev/null -w "gateway /api/chunks/  -> %{http_code}\n" -L "http://127.0.0.1:8888/api/chunks/"
curl -s -o /dev/null -w "gateway /api/catalog/browse -> %{http_code}\n" "http://127.0.0.1:8888/api/catalog/browse?limit=1"
curl -s "http://127.0.0.1:8888/api/orchestrator/state" | python3 -c "import sys,json; d=json.load(sys.stdin); print('orchestrator running:', d['running'], ' htr running:', len(d['htr']['running']))"
# regression: the other three independent services still serve
curl -s -o /dev/null -w "gateway /api/search/?q=x -> %{http_code}\n" "http://127.0.0.1:8888/api/search/?q=x&limit=1"
curl -s -o /dev/null -w "gateway /api/ray/health -> %{http_code}\n" "http://127.0.0.1:8888/api/ray/health"
```
Expected: `/api/batches/`, `/api/chunks/`, `/api/catalog/browse` → `200` (served by core-api over the `core` brick); `/api/orchestrator/state` → running True with HTR jobs in flight (the loop still ticks); search + ray still `200`. If `core-api` or `orchestrator` failed to boot, check `/tmp/rask-fleet.log` for an import error and fix.

- [ ] **Step 3: Done** — no commit (runtime check only).

---

## Done criteria

- The `viewer` package is GONE (`import viewer` fails); a new `core` brick owns the DB/models/alembic/domain/endpoints/lifespan/monolith.
- `core-api` + `orchestrator` import only from `core` + `service_kit`; their manifests + projects reference `core`, not `viewer`. `projects/viewer` deleted.
- `grep` finds NO `viewer` package reference anywhere (only `RASK_VIEWER_*` / `viewer_input` config names survive).
- Full `not slow` suite green at the same count (111); alembic runs via `--package core`; `ty`/`ruff` clean of new findings; the live fleet serves batches/chunks/catalog/orchestrator and the loop still submits HTR.

## Out of scope (follow-up)

The Helm per-service deployment cycle (Deployment+Service per service, gateway env-routing, the core-api/orchestrator 1-vs-2-process topology decision, `viewer-deployment.yaml` → core), then the NATS JetStream consumer that replaces the in-process orchestrator loop.
