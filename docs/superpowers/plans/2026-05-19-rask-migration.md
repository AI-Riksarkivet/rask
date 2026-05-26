# ra-batch → rask Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the entire ra-batch project into the rask monorepo under a Polylith-inspired `libraries/ · components/ · projects/` structure, dropping the `ra-`/`ra_` prefix and re-prefixing env vars `RA_`→`RASK_`.

**Architecture:** Clean copy from `/home/morgan/ra-batch` into `/home/morgan/rask` (no git history). rask root gains a uv-workspace `pyproject.toml`; `libraries/*` + `components/apps/*` + `components/services/*` are workspace members; each `projects/<name>` is a code-less composition project. ra-batch's `[tool.ruff]`/`[tool.ty]`/pytest config becomes the rask Python root standard.

**Tech Stack:** Python 3.13 (uv workspace, hatchling, ruff, ty, pytest), Ray Data/Serve, FastAPI, SvelteKit (bun), prek.

**Conventions for every commit in this plan:**

- Run inside `/home/morgan/rask` (the rask repo). Source files are read from `/home/morgan/ra-batch`.
- git identity is already `carpelan <m+github@carpelan.se>` (set locally in rask).
- Every commit message ends with a trailer line: `Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>`
- Conventional-commit subject prefixes only (`feat`/`fix`/`docs`/`refactor`/`test`/`chore`).
- **Never `git push`.** Stop after committing.
- Standard commit form used throughout:
  ```bash
  git -C /home/morgan/rask add -A
  git -C /home/morgan/rask commit -m "<type>: <subject>" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
  ```

**Baseline note:** ra-batch has two pre-existing failing tests unrelated to this migration (`test_page_loader_actor_reads_bytes`, `test_layout_actor_smoke`). Task 1 captures the ra-batch baseline so the migrated suite is judged against it (no _new_ failures), not against zero failures.

---

### Task 1: rask root pyproject + workspace skeleton + baseline capture

**Files:**

- Create: `/home/morgan/rask/pyproject.toml`
- Create: `/home/morgan/rask/libraries/.gitkeep`, `/home/morgan/rask/components/apps/.gitkeep`, `/home/morgan/rask/components/services/.gitkeep`, `/home/morgan/rask/components/scripts/.gitkeep`
- (exists already: `/home/morgan/rask/projects/.gitkeep`)

- [ ] **Step 1: Capture ra-batch test baseline**

Run:

```bash
cd /home/morgan/ra-batch && uv run pytest -q -m "not slow" 2>&1 | tail -5
```

Expected: a summary line like `N passed, M failed, ...`. Record N/M — the migrated suite must match (same failures, no new ones).

- [ ] **Step 2: Create the workspace skeleton dirs**

Run:

```bash
cd /home/morgan/rask
mkdir -p libraries components/apps components/services components/scripts
touch libraries/.gitkeep components/apps/.gitkeep components/services/.gitkeep components/scripts/.gitkeep
```

- [ ] **Step 3: Create `/home/morgan/rask/pyproject.toml`**

```toml
[project]
name = "rask-workspace"
version = "0.1.0"
description = "rask monorepo — uv workspace root (no entry points; see projects/*)."
requires-python = ">=3.13"
license = "Apache-2.0"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
bypass-selection = true

[tool.uv.workspace]
members = ["libraries/*", "components/apps/*", "components/services/*"]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.25",
    "pytest-cov>=7.0",
    "moto[s3]>=5.0",
    "ruff",
    "ty",
    "datasets>=4.8.4",
    "pylance>=0.20",
    "lancedb>=0.20",
]

[tool.ruff]
line-length = 160

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "S", "C4", "SIM", "RUF", "C901", "RET", "PERF", "FURB", "A", "ANN"]
ignore = ["S101", "S104", "ANN001", "ANN003", "ANN204", "RET504"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["S101", "ANN201", "ANN202"]
"components/scripts/harvest_ead.py" = ["ANN", "RET503", "B007"]
"components/scripts/index_catalog.py" = ["ANN", "RET503", "PERF401", "C901", "F841"]

[tool.ruff.lint.mccabe]
max-complexity = 15

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.ruff.lint.isort]
lines-after-imports = 2
known-first-party = ["htr", "storage", "runner", "viewer"]

[tool.ty.terminal]
error-on-warning = true

[tool.ty.environment]
python-version = "3.13"

[tool.pytest.ini_options]
testpaths = ["libraries/htr/tests", "libraries/storage/tests", "components/services/viewer/tests", "components/apps/runner/tests"]
addopts = "--cov --cov-report=term-missing:skip-covered --import-mode=importlib"
markers = ["slow: marks tests requiring real models or long runtimes (deselect with '-m \"not slow\"')"]

[tool.coverage.run]
branch = true
source = ["libraries/", "components/"]
omit = ["**/tests/*", "**/__init__.py"]
```

- [ ] **Step 4: Verify uv accepts the empty workspace**

Run:

```bash
cd /home/morgan/rask && uv sync 2>&1 | tail -3
```

Expected: resolves successfully (no members yet is fine), creates `.venv`.

- [ ] **Step 5: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "chore: add uv workspace root + Polylith folder skeleton" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 2: libraries/storage (was packages/ra-storage)

`ra_storage` is consumed publicly as `from ra_storage import ...` (by runner, viewer) and internally with absolute imports `from ra_storage.X import ...`.

**Files:**

- Create: `/home/morgan/rask/libraries/storage/` (copied from `/home/morgan/ra-batch/packages/ra-storage/`)
- Rename inside: `src/ra_storage/` → `src/storage/`
- Modify: `libraries/storage/pyproject.toml`, all `*.py` under `libraries/storage/`

- [ ] **Step 1: Copy the package and rename the source dir**

Run:

```bash
cd /home/morgan/rask
cp -r /home/morgan/ra-batch/packages/ra-storage libraries/storage
git -C /home/morgan/rask mv libraries/storage/src/ra_storage libraries/storage/src/storage 2>/dev/null || mv libraries/storage/src/ra_storage libraries/storage/src/storage
```

- [ ] **Step 2: Rewrite `ra_storage` identifiers in code**

Run:

```bash
cd /home/morgan/rask
grep -rl --include='*.py' -E '\bra_storage\b' libraries/storage | xargs sed -i 's/\bra_storage\b/storage/g'
```

- [ ] **Step 3: Rewrite `libraries/storage/pyproject.toml`**

Replace the `[project] name` line and the wheel-packages line. Final file:

```toml
[project]
name = "storage"
version = "0.1.0"
description = "Filesystem and S3/HCP source/sink helpers used by runner and viewer."
requires-python = ">=3.10"
license = "Apache-2.0"
dependencies = [
    "boto3>=1.35",
    "httpx>=0.27",
    "python-dotenv>=1.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/storage"]
```

- [ ] **Step 4: Sync and run storage tests**

Run:

```bash
cd /home/morgan/rask && uv sync && uv run pytest libraries/storage/tests -q 2>&1 | tail -5
```

Expected: all `libraries/storage/tests` pass (storage had no pre-existing failures in baseline).

- [ ] **Step 5: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: migrate ra-storage to libraries/storage" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 3: libraries/htr (was packages/ra-htr)

Depends on `storage` (Task 2). Internal absolute imports `from ra_htr.X import ...`; depends on `ra-storage`.

**Files:**

- Create: `/home/morgan/rask/libraries/htr/` (from `/home/morgan/ra-batch/packages/ra-htr/`)
- Rename: `src/ra_htr/` → `src/htr/`
- Modify: `libraries/htr/pyproject.toml`, all `*.py` under `libraries/htr/`

- [ ] **Step 1: Copy and rename source dir**

Run:

```bash
cd /home/morgan/rask
cp -r /home/morgan/ra-batch/packages/ra-htr libraries/htr
mv libraries/htr/src/ra_htr libraries/htr/src/htr
```

- [ ] **Step 2: Rewrite `ra_htr` and `ra_storage` identifiers**

Run:

```bash
cd /home/morgan/rask
grep -rl --include='*.py' -E '\bra_htr\b|\bra_storage\b' libraries/htr \
  | xargs sed -i -e 's/\bra_htr\b/htr/g' -e 's/\bra_storage\b/storage/g'
```

- [ ] **Step 3: Rewrite `libraries/htr/pyproject.toml`**

Final file:

```toml
[project]
name = "htr"
version = "0.1.0"
description = "HTR actors (layout, lines, TrOCR, ALTO) for use with Ray Data."
requires-python = ">=3.13"
license = "Apache-2.0"
dependencies = [
    "storage",
    "pillow>=11.0",
    "numpy>=2.0",
    "torch==2.8.0",
    "transformers>=5.6.1",
    "ultralytics>=8.4.41",
    "huggingface-hub>=0.28",
    "jinja2>=3.1",
    "accelerate>=1.13.0",
    "opencv-python>=4.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/htr"]

[tool.uv.sources]
storage = { workspace = true }
```

- [ ] **Step 4: Sync and run htr tests (non-slow)**

Run:

```bash
cd /home/morgan/rask && uv sync && uv run pytest libraries/htr/tests -q -m "not slow" 2>&1 | tail -6
```

Expected: pass/fail count matches the ra-batch baseline for `packages/ra-htr/tests` (the two known pre-existing failures may appear here; **no new failures**). If a failure is an `ImportError`/`ModuleNotFoundError` for `ra_htr`/`ra_storage`, a rename was missed — fix and re-run.

- [ ] **Step 5: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: migrate ra-htr to libraries/htr" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 4: components/apps/runner (was apps/ra-runner)

Depends on `htr` + `storage`. Console script `ra-runner = ra_runner.main:app`. Keeps the local `htrflow` path source (`/home/morgan/htrflow`, verified present).

**Files:**

- Create: `/home/morgan/rask/components/apps/runner/` (from `/home/morgan/ra-batch/apps/ra-runner/`)
- Rename: `src/ra_runner/` → `src/runner/`
- Modify: `components/apps/runner/pyproject.toml`, all `*.py` under `components/apps/runner/`

- [ ] **Step 1: Copy and rename source dir**

Run:

```bash
cd /home/morgan/rask
cp -r /home/morgan/ra-batch/apps/ra-runner components/apps/runner
mv components/apps/runner/src/ra_runner components/apps/runner/src/runner
```

- [ ] **Step 2: Rewrite `ra_runner` / `ra_htr` / `ra_storage` identifiers**

Run:

```bash
cd /home/morgan/rask
grep -rl --include='*.py' -E '\bra_runner\b|\bra_htr\b|\bra_storage\b' components/apps/runner \
  | xargs sed -i -e 's/\bra_runner\b/runner/g' -e 's/\bra_htr\b/htr/g' -e 's/\bra_storage\b/storage/g'
```

- [ ] **Step 3: Rewrite `components/apps/runner/pyproject.toml`**

Final file:

```toml
[project]
name = "runner"
version = "0.1.0"
description = "Ray Data batch driver for HTR over htr actors."
requires-python = ">=3.13"
license = "Apache-2.0"
dependencies = [
    "htr",
    "storage",
    "ray[data,default,serve]>=2.52,<2.56",
    "rich>=13.7",
    "typer>=0.12",
    "python-dotenv>=1.0",
    "htrflow",
]

[project.scripts]
runner = "runner.main:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/runner"]

[tool.uv.sources]
htr = { workspace = true }
storage = { workspace = true }
htrflow = { path = "/home/morgan/htrflow", editable = false }
```

- [ ] **Step 4: Sync, run runner tests, smoke the CLI**

Run:

```bash
cd /home/morgan/rask && uv sync && uv run pytest components/apps/runner/tests -q -m "not slow" 2>&1 | tail -6
uv run runner --help 2>&1 | tail -3
```

Expected: runner tests match baseline (no new failures); `runner --help` prints the typer help including `--pipeline (htr|htrflow|prefetch|fake)`.

- [ ] **Step 5: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: migrate ra-runner to components/apps/runner" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 5: components/services/viewer (was apps/ra-viewer)

Drops the vestigial `ra-batch` root dependency (viewer imports only `ra_storage` + stdlib + fastapi/httpx/lance/uvicorn/boto3 — verified, no `ra_runner`/`ra_htr` imports). Renames env vars `RA_VIEWER_INPUT`/`RA_VIEWER_OUTPUT`/`RA_SEARCH_BUCKET` → `RASK_*`.

**Files:**

- Create: `/home/morgan/rask/components/services/viewer/` (from `/home/morgan/ra-batch/apps/ra-viewer/`)
- Rename: `src/ra_viewer/` → `src/viewer/`
- Modify: `components/services/viewer/pyproject.toml`, all `*.py` under `components/services/viewer/`

- [ ] **Step 1: Copy and rename source dir**

Run:

```bash
cd /home/morgan/rask
cp -r /home/morgan/ra-batch/apps/ra-viewer components/services/viewer
mv components/services/viewer/src/ra_viewer components/services/viewer/src/viewer
```

- [ ] **Step 2: Rewrite identifiers and env var names**

Run:

```bash
cd /home/morgan/rask
grep -rl --include='*.py' -E '\bra_viewer\b|\bra_storage\b|RA_VIEWER_INPUT|RA_VIEWER_OUTPUT|RA_SEARCH_BUCKET' components/services/viewer \
  | xargs sed -i \
    -e 's/\bra_viewer\b/viewer/g' \
    -e 's/\bra_storage\b/storage/g' \
    -e 's/RA_VIEWER_INPUT/RASK_VIEWER_INPUT/g' \
    -e 's/RA_VIEWER_OUTPUT/RASK_VIEWER_OUTPUT/g' \
    -e 's/RA_SEARCH_BUCKET/RASK_SEARCH_BUCKET/g'
```

- [ ] **Step 3: Rewrite `components/services/viewer/pyproject.toml`**

Final file (note: `ra-batch` dependency removed; concrete deps listed):

```toml
[project]
name = "viewer"
version = "0.1.0"
description = "FastAPI backend for the viewer — proxies images/ALTO from object storage."
requires-python = ">=3.13"
license = "Apache-2.0"
dependencies = [
    "storage",
    "fastapi>=0.115",
    "httpx>=0.27",
    "uvicorn>=0.30",
    "python-dotenv>=1.0",
    "pylance>=0.20",
]

[project.scripts]
viewer = "viewer.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/viewer"]

[tool.uv.sources]
storage = { workspace = true }
```

- [ ] **Step 4: Sync and run viewer tests**

Run:

```bash
cd /home/morgan/rask && uv sync && uv run pytest components/services/viewer/tests -q 2>&1 | tail -5
```

Expected: `components/services/viewer/tests` pass (the smoke test sets `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` after the rename in Step 2).

- [ ] **Step 5: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: migrate ra-viewer to components/services/viewer" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 6: components/scripts/ (was scripts/)

Standalone runnable utilities — not a uv member. Fix `ra_*` imports and remaining `RA_*` env names (`RA_ORCHESTRATOR_SKIP_HTR`, `RA_BATCH_MASTER_CSV`, `RA_SEARCH_BUCKET`).

**Files:**

- Create: `/home/morgan/rask/components/scripts/` (from `/home/morgan/ra-batch/scripts/`, excluding `__pycache__`)
- Modify: all `*.py` under `components/scripts/`

- [ ] **Step 1: Copy scripts (no pycache)**

Run:

```bash
cd /home/morgan/rask
mkdir -p components/scripts
rsync -a --exclude '__pycache__' /home/morgan/ra-batch/scripts/ components/scripts/
```

- [ ] **Step 2: Rewrite identifiers + env var names**

Run:

```bash
cd /home/morgan/rask
grep -rl --include='*.py' -E '\bra_storage\b|\bra_htr\b|\bra_runner\b|\bra_viewer\b|RA_ORCHESTRATOR_SKIP_HTR|RA_BATCH_MASTER_CSV|RA_SEARCH_BUCKET' components/scripts \
  | xargs sed -i \
    -e 's/\bra_storage\b/storage/g' -e 's/\bra_htr\b/htr/g' \
    -e 's/\bra_runner\b/runner/g' -e 's/\bra_viewer\b/viewer/g' \
    -e 's/RA_ORCHESTRATOR_SKIP_HTR/RASK_ORCHESTRATOR_SKIP_HTR/g' \
    -e 's/RA_BATCH_MASTER_CSV/RASK_BATCH_MASTER_CSV/g' \
    -e 's/RA_SEARCH_BUCKET/RASK_SEARCH_BUCKET/g'
```

- [ ] **Step 3: Byte-compile every script + lint**

Run:

```bash
cd /home/morgan/rask
uv run python -m compileall -q components/scripts && echo COMPILE_OK
uv run ruff check components/scripts 2>&1 | tail -3
```

Expected: `COMPILE_OK`; ruff passes (the two vendored scripts are covered by the `per-file-ignores` added in Task 1).

- [ ] **Step 4: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: migrate scripts to components/scripts" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 7: projects/runner + projects/viewer (code-less composition)

Each is a non-member uv project whose pyproject composes workspace bricks.

**Files:**

- Create: `/home/morgan/rask/projects/runner/pyproject.toml`
- Create: `/home/morgan/rask/projects/viewer/pyproject.toml`

- [ ] **Step 1: Create `projects/runner/pyproject.toml`**

```toml
[project]
name = "runner-project"
version = "0.1.0"
description = "Deployable: runner CLI + Ray Data HTR pipeline."
requires-python = ">=3.13"
dependencies = ["runner"]

[tool.uv.sources]
runner = { workspace = true }
htrflow = { path = "/home/morgan/htrflow", editable = false }

[tool.uv.workspace]
members = []
```

- [ ] **Step 2: Create `projects/viewer/pyproject.toml`**

```toml
[project]
name = "viewer-project"
version = "0.1.0"
description = "Deployable: FastAPI viewer service."
requires-python = ">=3.13"
dependencies = ["viewer"]

[tool.uv.sources]
viewer = { workspace = true }

[tool.uv.workspace]
members = []
```

- [ ] **Step 3: Resolve each project against the workspace**

Run:

```bash
cd /home/morgan/rask
uv sync --project projects/runner 2>&1 | tail -2
uv run --project projects/runner runner --help 2>&1 | tail -2
uv sync --project projects/viewer 2>&1 | tail -2
```

Expected: both resolve; `runner --help` prints help. If uv reports `runner`/`viewer` not found, the workspace member glob (Task 1) or the brick pyproject name (Task 4/5) is wrong — fix there.

- [ ] **Step 4: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "feat: add projects/runner + projects/viewer composition" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 8: Re-point the Ray runtime_env in submit_chunks

ra-batch's `components/scripts/submit_chunks.py` builds `uv run ra-runner …` with `working_dir = REPO`. Re-point to the renamed CLI via the project, and set `REPO` to the rask root.

**Files:**

- Modify: `/home/morgan/rask/components/scripts/submit_chunks.py`

- [ ] **Step 1: Inspect the current entrypoint + REPO**

Run:

```bash
cd /home/morgan/rask
grep -n 'uv run ra-runner\|^REPO\|REPO *=\|working_dir=REPO\|startswith(("AWS_"' components/scripts/submit_chunks.py
```

Expected: shows the `"uv run ra-runner"` string (in `build_entrypoint`), the `REPO = …` definition, `working_dir=REPO`, and the env_vars passthrough filter.

- [ ] **Step 2: Re-point the entrypoint command**

Edit `components/scripts/submit_chunks.py`: change the entrypoint base string from
`"uv run ra-runner"` to `"uv run --project projects/runner runner"`.

- [ ] **Step 3: Point REPO at the rask root**

Edit the `REPO` definition so it resolves to `/home/morgan/rask` (the repo root that contains `projects/runner` and the uv workspace). Use the existing pattern in the file — it derives REPO from `__file__`; since the script moved one level deeper is _not_ the case (it was `scripts/`, now `components/scripts/`), adjust the parent count so `REPO` is the rask root. Verify in Step 5.

- [ ] **Step 4: Add RASK\_ to the env_vars passthrough**

Edit the env*vars passthrough filter so it also forwards `RASK*`-prefixed vars:
change `.startswith(("AWS*", "HCP*", "IIIF*"))`to`.startswith(("AWS*", "HCP*", "IIIF*", "RASK\_"))`.

- [ ] **Step 5: Verify the generated job spec (dry, no submission)**

Run:

```bash
cd /home/morgan/rask
uv run python - <<'PY'
import importlib.util, pathlib
p = pathlib.Path("components/scripts/submit_chunks.py")
spec = importlib.util.spec_from_file_location("submit_chunks", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("REPO =", m.REPO)
print("entrypoint =", m.build_entrypoint(["A0060198"], **({} )) if False else "see grep")
PY
grep -n 'uv run --project projects/runner runner\|RASK_\|REPO' components/scripts/submit_chunks.py | head
test "$(cd /home/morgan/rask && uv run python -c 'import importlib.util,pathlib;p=pathlib.Path("components/scripts/submit_chunks.py");s=importlib.util.spec_from_file_location("sc",p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.REPO)')" = "/home/morgan/rask" && echo REPO_OK
```

Expected: `REPO_OK`; the entrypoint string is now `uv run --project projects/runner runner`; `RASK_` present in the passthrough.

- [ ] **Step 6: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "fix: re-point Ray runtime_env to projects/runner in submit_chunks" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 9: Root wiring — Makefile, bun workspace, frontend, .claude

**Files:**

- Modify: `/home/morgan/rask/Makefile` (merge ra-batch targets into rask's)
- Modify: `/home/morgan/rask/package.json` (add frontend to bun workspaces)
- Create: `/home/morgan/rask/components/apps/frontend/` (from `/home/morgan/ra-batch/frontend/`)
- Create: `/home/morgan/rask/.claude/` (from `/home/morgan/ra-batch/.claude/`)

- [ ] **Step 1: Copy the frontend (no build artefacts / node_modules)**

Run:

```bash
cd /home/morgan/rask
rsync -a --exclude node_modules --exclude .svelte-kit --exclude build /home/morgan/ra-batch/frontend/ components/apps/frontend/
sed -i 's/"name": "ra-viewer-frontend"/"name": "viewer-frontend"/' components/apps/frontend/package.json
```

- [ ] **Step 2: Add frontend to rask's bun workspaces**

Edit `/home/morgan/rask/package.json`: in the `"workspaces"` array, add `"components/apps/frontend"` alongside the existing `"compontens/apps/webapp"` and `"packages/oxen_componets"`. Leave everything else unchanged.

- [ ] **Step 3: Copy .claude/**

Run:

```bash
cd /home/morgan/rask
rsync -a /home/morgan/ra-batch/.claude/ .claude/
```

- [ ] **Step 4: Merge ra-batch Makefile targets into rask's root Makefile**

Append the following ra-batch-derived targets to `/home/morgan/rask/Makefile` (paths/env updated for the new layout; rask's existing `help/install/build/test/lint/fmt/storybook/clean` are kept as-is, and the `.PHONY` line extended with the new target names):

```makefile
# ---- python (uv workspace) -------------------------------------------------
typecheck:
	uvx ty check

check: fmt lint typecheck

ci: check test

# ---- viewer ----------------------------------------------------------------
# Port must be 8888 — components/apps/frontend Vite proxy defaults
# VIEWER_BACKEND to http://localhost:8888.
VIEWER_INPUT  ?= s3://images-batch
VIEWER_OUTPUT ?= s3://images-batch-alto

viewer:
	RASK_VIEWER_INPUT=$(VIEWER_INPUT) RASK_VIEWER_OUTPUT=$(VIEWER_OUTPUT) \
		uv run uvicorn viewer.app:app --host 0.0.0.0 --port 8888 --reload

viewer-frontend:
	cd components/apps/frontend && bun run dev

viewer-frontend-build:
	cd components/apps/frontend && bun install && bun run build

# ---- ray -------------------------------------------------------------------
RAY_HEAD_PORT       ?= 6379
RAY_DASHBOARD_PORT  ?= 8265

ray-up:
	@if ray status >/dev/null 2>&1; then \
	  echo "Ray already running. ray-status / ray-down to inspect / stop."; \
	else \
	  uv run ray start --head --port=$(RAY_HEAD_PORT) \
	    --dashboard-host=0.0.0.0 --dashboard-port=$(RAY_DASHBOARD_PORT); \
	  echo "Ray dashboard: http://localhost:$(RAY_DASHBOARD_PORT)"; \
	fi

ray-down:
	uv run ray stop

ray-status:
	uv run ray status

# ---- serve -----------------------------------------------------------------
serve-up:
	uv run python components/scripts/deploy_serve.py up

serve-down:
	uv run python components/scripts/deploy_serve.py down

serve-status:
	uv run python components/scripts/deploy_serve.py status

# ---- search / catalog index ------------------------------------------------
search-index:
	uv run python components/scripts/submit_index.py

search-index-fresh:
	uv run python components/scripts/submit_index.py --skip-existing

harvest-ead:
	uv run python components/scripts/harvest_ead.py

catalog-index:
	uv run python components/scripts/index_catalog.py --no-embed --digitized-only
```

Also add to the existing `.PHONY:` line: `typecheck check ci viewer viewer-frontend viewer-frontend-build ray-up ray-down ray-status serve-up serve-down serve-status search-index search-index-fresh harvest-ead catalog-index`.

- [ ] **Step 5: Verify wiring**

Run:

```bash
cd /home/morgan/rask
make -n viewer | grep -q 'RASK_VIEWER_INPUT' && echo MAKE_VIEWER_OK
make -n typecheck >/dev/null && echo MAKE_TYPECHECK_OK
bun install 2>&1 | tail -2
python -c "import json;w=json.load(open('package.json'))['workspaces'];assert 'components/apps/frontend' in w, w;print('WORKSPACES_OK')"
ls .claude/commands/commit.md .claude/hooks/pre-commit >/dev/null && echo CLAUDE_OK
```

Expected: `MAKE_VIEWER_OK`, `MAKE_TYPECHECK_OK`, `WORKSPACES_OK`, `CLAUDE_OK`; `bun install` resolves the new workspace.

- [ ] **Step 6: Commit**

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "chore: wire root Makefile, bun workspace, frontend, .claude" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

### Task 10: Acceptance — full validation + real Ray chunk

**Files:** none modified (validation only; optional runbook note at end).

- [ ] **Step 1: Workspace-wide lint, type, test**

Run:

```bash
cd /home/morgan/rask
make lint 2>&1 | tail -3
make typecheck 2>&1 | tail -3
make test 2>&1 | tail -6
```

Expected: lint clean; ty clean; pytest pass/fail count equals the Task 1 ra-batch baseline (same known failures, **zero new failures**).

- [ ] **Step 2: Identifier / env-var purge gate**

Run:

```bash
cd /home/morgan/rask
grep -rn --include='*.py' --include='*.toml' -E '\bra_(storage|htr|runner|viewer)\b' libraries components projects && echo "FAIL: ra_ identifiers remain" || echo IDENT_CLEAN
grep -rn --include='*.py' --include='*.toml' --include='Makefile' -E '\bRA_[A-Z_]+' libraries components projects Makefile && echo "FAIL: RA_ env names remain" || echo ENV_CLEAN
grep -rn -E 'name *= *"ra-(storage|htr|runner|viewer)"' libraries components projects && echo "FAIL: ra- dist names remain" || echo DIST_CLEAN
```

Expected: `IDENT_CLEAN`, `ENV_CLEAN`, `DIST_CLEAN`.

- [ ] **Step 3: CLI + project composition smoke**

Run:

```bash
cd /home/morgan/rask
uv run --project projects/runner runner --help 2>&1 | tail -2
uv sync --project projects/viewer 2>&1 | tail -1
```

Expected: `runner --help` prints; viewer project resolves.

- [ ] **Step 4: Viewer service boot check**

Run:

```bash
cd /home/morgan/rask
( make viewer & echo $! > /tmp/rask_viewer.pid ) ; sleep 8
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8888/api/volumes
kill "$(cat /tmp/rask_viewer.pid)" 2>/dev/null; pkill -f 'uvicorn viewer.app:app' 2>/dev/null || true
```

Expected: HTTP code is not `500` (a `200` or a clean `4xx` proves env vars wired + app importable as `viewer.app:app`).

- [ ] **Step 5: Real Ray chunk end-to-end (proves runtime_env re-point)**

Pre-req: a local Ray cluster is up (`make ray-up`) and MinIO/HCP creds are in the environment (same as ra-batch runs). Submit one small chunk:

```bash
cd /home/morgan/rask
uv run python components/scripts/submit_chunks.py --limit 1 2>&1 | tail -20
```

Then watch the submitted job in the Ray dashboard (`http://localhost:8265`) until it reaches `SUCCEEDED`, and confirm at least one ALTO `.xml` was written to the configured output bucket/prefix.
Expected: job `SUCCEEDED`, ≥1 ALTO object written. If the job fails at `uv sync` inside the Ray working_dir, the Task 8 re-point (`--project projects/runner`, `REPO`) is wrong — fix and re-run.

- [ ] **Step 6: Final commit (runbook note)**

Append a short "Migration complete — validated <date>" note to `docs/superpowers/specs/2026-05-19-rask-migration-design.md` under a new `## Status` line, then:

```bash
git -C /home/morgan/rask add -A
git -C /home/morgan/rask commit -m "docs: mark ra-batch→rask migration validated" -m "Co-Authored-By: Borg93 <Borg93@users.noreply.github.com>"
```

---

## Self-Review

**Spec coverage:**

- Structure (libraries/components{apps,services,scripts}/projects) → Tasks 1–7 ✓
- `ra-`/`ra_` drop (dist, modules, console script, sources, imports, tests) → Tasks 2–6, gate in Task 10 Step 2 ✓
- `RA_`→`RASK_` env vars (VIEWER_INPUT/OUTPUT, SEARCH_BUCKET, ORCHESTRATOR_SKIP_HTR, BATCH_MASTER_CSV) → Tasks 5–6, gate Task 10 ✓
- Ray runtime_env re-point → Task 8, validated Task 10 Step 5 ✓
- Tooling: ruff/ty/pytest/coverage ported to rask root → Task 1 ✓
- uv workspace + code-less projects → Tasks 1, 7 ✓
- bun workspace + frontend → Task 9 ✓
- Makefile merge, .claude → Task 9 ✓
- k8s skipped, docs/superpowers history dropped → not copied (absent from all tasks) ✓
- Commit strategy (conventional + Borg93 trailer, no push) → every task ✓
- Acceptance criteria → Task 10 ✓

**Placeholder scan:** No TBD/TODO; every code/config step shows full content; commands have expected output. Task 8 Step 3 references "the existing pattern in the file" but pins the outcome with an automated `REPO_OK` assertion in Step 5 — concrete gate, not a placeholder.

**Type/name consistency:** Brick dist names (`storage`, `htr`, `runner`, `viewer`) are consistent across pyprojects, `[tool.uv.sources]`, workspace members, pytest `testpaths`, isort `known-first-party`, and `projects/*`. Console script renamed `ra-runner`→`runner` consistently (Task 4 + projects/runner + submit_chunks Task 8 + Makefile/acceptance).
