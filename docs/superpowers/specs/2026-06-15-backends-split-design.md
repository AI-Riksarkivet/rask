# Split `backends` into per-service bricks + deployables

**Date:** 2026-06-15
**Status:** Approved (design), pending implementation plan
**Scope:** Packaging refactor only. No business logic changes. Chart work deferred to a follow-up step.

## Problem

`components/services/backends` is a single brick housing six independently-runnable
FastAPI apps (`gateway`, `core_api`, `search_api`, `volumes_api`, `ray_api`,
`orchestrator`) as flat modules under `src/backends/`. They ship as one workspace
member and one deployable (`projects/backends`). We want each backend to be its own
brick **and** its own deployable, so the Helm chart can build and scale each service
independently.

## Current state (facts that shape the design)

- **No business logic lives in the backends brick.** Each service module is 7–15 lines
  that compose *viewer's* routers, e.g. `from viewer.api.v1.endpoints import health, search`.
  The route handlers stay in `components/services/viewer`.
- **`_common.py`** holds `make_service_app` (+ `build_settings`, `_setup_logging`). It
  depends on `viewer.core.*` and `storage`.
- **Dependency graph is lopsided:**
  - `core_api`, `search_api`, `volumes_api`, `ray_api`, `orchestrator` each need
    `_common` + the whole `viewer` package + `storage`. Their dep sets are nearly identical.
  - `gateway` (138 lines) depends on **nothing internal** — only `httpx`, `fastapi`,
    `python-dotenv`. It is a pure HTTP proxy and the `:8888` origin the SPA targets.
- The genuine payoff of the split is at the **deployable** level (independent container
  images / scaling), not the dependency level — the five viewer-based bricks all pull in
  `viewer` regardless.

## Target layout

```
packages/
  service-kit/                      # was _common.py
    pyproject.toml                  #   deps: fastapi, python-dotenv, viewer, storage
    src/service_kit/__init__.py     #   make_service_app, build_settings, _setup_logging

components/services/
  gateway/      src/gateway/__init__.py        # app — deps: fastapi, httpx, python-dotenv (NO viewer)
  core_api/     src/core_api/__init__.py        # app — deps: service-kit, viewer
  search_api/   src/search_api/__init__.py
  volumes_api/  src/volumes_api/__init__.py
  ray_api/      src/ray_api/__init__.py
  orchestrator/ src/orchestrator/__init__.py

projects/
  gateway/  core-api/  search-api/  volumes-api/  ray-api/  orchestrator/
                                    # one deployable composition each
```

**Deleted:** `components/services/backends/`, `projects/backends/`.

## Decisions

- **`_common` → `packages/service-kit`** (a library brick, no entrypoint), per the polylith
  rule that `packages/` holds reusable libraries. The five viewer-based bricks depend on it;
  `gateway` does not.
- **Import package names keep the underscore module names** (`core_api`, `search_api`,
  `volumes_api`, `ray_api`, `orchestrator`, `gateway`). App paths become `core_api:app`,
  `search_api:app`, … `gateway:app`. Minimal churn; paths stay obvious.
- **Each backend exposes `app` from its package `__init__.py`** (one-file packages).
- **Chart deployment is a separate follow-up step**, not part of this refactor. Backends are
  not yet deployed in `chart/` (only `viewer-deployment.yaml` exists), and that chart is
  under active development. Keeping the packaging refactor isolated avoids tangling it with
  greenfield chart work.

## Wiring changes (the real work)

1. **Root `pyproject.toml`** `[tool.uv.workspace] members`: remove
   `components/services/backends`; add `packages/service-kit` and the six new bricks.
2. **Six `projects/<name>/pyproject.toml`** deployable compositions, modelled on the existing
   `projects/backends/pyproject.toml`:
   - viewer-based services depend on their brick + `viewer` + `storage` + `service-kit`
     (workspace sources).
   - `gateway` depends only on its own brick.
3. **`dev-micro.sh`**: update the six `run` lines from `backends.<mod>:app` to `<mod>:app`.
4. **`Makefile`**: update any `backends`-specific references (e.g. `dev-micro`).

## Non-goals

- No change to route handlers, viewer code, or runtime behaviour.
- No change to the gateway's routing table or OpenAPI merge.
- No chart templates in this pass (tracked as the follow-up step).

## Verification

- `uv sync --all-packages` resolves the new workspace cleanly.
- Each app imports:
  `uv run --no-sync python -c "import core_api, search_api, volumes_api, ray_api, orchestrator, gateway"`.
- `make dev-micro` brings the fleet up; `curl :8888/api/v1/docs` shows the merged OpenAPI
  across all backends.
- `uvx ty check` passes (project runs `error-on-warning = true`).
- Existing `viewer` test suite is unaffected (`make test`).

## Follow-up (separate step)

Chart deployment: add a Deployment + Service per backend and a gateway Deployment + Service,
plus a `values.yaml` backends section, with `gateway` as the `:8888` origin the frontend
targets. Coordinated with the in-flight `chart/` work.
