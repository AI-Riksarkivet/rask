# rask Docker Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the entire rask fleet (gateway + 5 backend services + frontend) plus its infrastructure (MinIO, Postgres, Ray + htrflow Serve) on this machine via a single `docker-compose.yml`.

**Architecture:** Per-service Docker images modeled on the existing `viewer.dockerfile` two-stage uv pattern; a GPU `ray-head` image derived from `runner.dockerfile` rebased onto an arm64 CUDA-13 base; one `docker-compose.yml` wiring it all with named volumes, `--gpus all`, one-shot `migrate`/`minio-init` jobs, and healthcheck-gated startup ordering. `make compose-*` targets drive it. Coexists with the existing Makefile/`dev-micro.sh` dev runbook.

**Tech Stack:** Docker Compose v2, docker buildx (arm64), uv, FastAPI/uvicorn, Ray Serve, MinIO, Postgres 16, Alembic.

## Global Constraints

- **Toolchain:** JS/TS via Bun; Python via uv (3.13). Builds use `docker buildx` (native arm64). No npm/npx.
- **Platform:** host is aarch64 + NVIDIA GB10 (sm_120), driver 580 / CUDA 13. Docker 29.5.3, `nvidia` runtime registered, `--gpus all` confirmed working.
- **Image tags:** all locally-built images tagged `:dev`; compose uses `pull_policy: never` for locally-built images.
- **Build context** for every Dockerfile is the **repo root** (Dockerfiles live in `.docker/`, referenced via `-f`).
- **Canonical service map** (project / module / port): gateway `gateway:app` 8888 · core-api `core_api:app` 8801 · search-api `search_api:app` 8802 · volumes-api `volumes_api:app` 8803 · ray-api `ray_api:app` 8804 · orchestrator `orchestrator:app` 8810.
- **Gateway upstream env vars:** `RASK_CORE_API_URL`, `RASK_SEARCH_API_URL`, `RASK_VOLUMES_API_URL`, `RASK_RAY_API_URL`, `RASK_ORCH_API_URL`.
- **Health route:** `GET /api/v1/health` on every FastAPI service (`api_prefix` default `/api/v1`).
- **Phase-1 flags (carried in compose env):** `RASK_SOURCE_MODE=s3`, `RASK_HTR_PIPELINE=htrflow`, `RASK_PREFETCH_PIPELINE=none`, `RASK_SERVE_REPLICAS=1`, `RASK_SERVE_GPU_FRAC=1.0`, `RASK_IIIF_URL=""`, `RASK_VIEWER_INPUT=s3://images-batch`, `RASK_VIEWER_OUTPUT=s3://images-batch-alto`.
- **Orchestrator autostart OFF:** `RASK_ORCHESTRATOR_AUTOSTART=false`.
- **Buckets:** `images-batch` (input) + `images-batch-alto` (output).
- Commits: **no Claude/AI co-author trailer.**

---

### Task 1: Per-service CPU Dockerfiles + retire viewer.dockerfile

Create six near-identical Dockerfiles (one per FastAPI service) from a single parameterized template, and delete the stale `viewer.dockerfile`. Each builds the service's minimal venv via `uv sync --package <project>` and runs `uvicorn <module>:app`.

**Files:**
- Create: `.docker/gateway.dockerfile`, `.docker/core-api.dockerfile`, `.docker/search-api.dockerfile`, `.docker/volumes-api.dockerfile`, `.docker/ray-api.dockerfile`, `.docker/orchestrator.dockerfile`
- Delete: `.docker/viewer.dockerfile`
- Create (helper): `.docker/smoke-build.sh` (build + run + health-probe one service; used as the task's test)

**Interfaces:**
- Produces: six images `gateway:dev`, `core-api:dev`, `search-api:dev`, `volumes-api:dev`, `ray-api:dev`, `orchestrator:dev`, each exposing its port and answering `GET /api/v1/health` with HTTP 200.
- Consumes: nothing (first task).

**Canonical template** (substitute `__PROJECT__`, `__MODULE__`, `__PORT__`, `__TITLE__`, `__HEALTHPATH__` per the table below):

```dockerfile
# syntax=docker/dockerfile:1.11
# rask __PROJECT__ image — FastAPI on python:3.13-slim-bookworm.
# Build from repo root:
#   docker buildx build -f .docker/__PROJECT__.dockerfile -t __PROJECT__:dev .

# ---- builder stage: install deps via uv ------------------------------------
# hadolint ignore=DL3026
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2 AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv

WORKDIR /app

# Step 1: install workspace deps (frozen — workspace member sources not yet COPYed).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=projects/__PROJECT__/pyproject.toml,target=projects/__PROJECT__/pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    --mount=type=bind,source=components,target=components \
    uv sync --frozen --no-install-workspace --package __PROJECT__ --no-editable

# Step 2: COPY real sources and resolve workspace deps (locked).
COPY pyproject.toml uv.lock ./
COPY packages    packages
COPY components  components
COPY projects/__PROJECT__ projects/__PROJECT__
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package __PROJECT__ --no-editable

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

# ---- final stage -----------------------------------------------------------
# hadolint ignore=DL3026
FROM python:3.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2

LABEL org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.title="rask-__PROJECT__" \
      org.opencontainers.image.description="__TITLE__"

# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -r --no-create-home --shell /usr/sbin/nologin --uid 10001 app

RUN --network=none --mount=from=builder,source=/opt/venv,target=/tmp/venv \
    cp -a /tmp/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE __PORT__

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:__PORT____HEALTHPATH__ || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "__MODULE__", "--host", "0.0.0.0", "--port", "__PORT__", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
```

Substitution table:

| File | `__PROJECT__` | `__MODULE__` | `__PORT__` | `__HEALTHPATH__` | `__TITLE__` |
|---|---|---|---|---|---|
| `gateway.dockerfile` | `gateway` | `gateway:app` | `8888` | `/api/v1/docs` | `rask gateway — reverse proxy on :8888` |
| `core-api.dockerfile` | `core-api` | `core_api:app` | `8801` | `/api/v1/health` | `rask core-api — batches/chunks/catalog on :8801` |
| `search-api.dockerfile` | `search-api` | `search_api:app` | `8802` | `/api/v1/health` | `rask search-api on :8802` |
| `volumes-api.dockerfile` | `volumes-api` | `volumes_api:app` | `8803` | `/api/v1/health` | `rask volumes-api on :8803` |
| `ray-api.dockerfile` | `ray-api` | `ray_api:app` | `8804` | `/api/v1/health` | `rask ray-api on :8804` |
| `orchestrator.dockerfile` | `orchestrator` | `orchestrator:app` | `8810` | `/api/v1/health` | `rask orchestrator on :8810` |

> Note: the **gateway has no local `/api/v1/health`** — it path-routes `/api/*` to backends, so `/api/v1/health` is *proxied* to core-api and can't answer in isolation. The gateway *does* serve `/api/v1/docs` locally (swagger HTML, HTTP 200, no upstream call), so that is its liveness path. The five backends each mount `health.router` → `/api/v1/health`. `--forwarded-allow-ips "*"` is acceptable here because the only published port is the gateway on a trusted local network; tighten for any non-local deployment.

- [ ] **Step 1: Write the smoke-test script (the failing test)**

Create `.docker/smoke-build.sh`:

```bash
#!/usr/bin/env bash
# Build one per-service image, run it, assert /api/v1/health returns 200.
# Usage: .docker/smoke-build.sh <project> <port> [healthpath]
set -euo pipefail
proj="$1"; port="$2"; healthpath="${3:-/api/v1/health}"
img="${proj}:dev"
echo ">> building ${img}"
docker buildx build -f ".docker/${proj}.dockerfile" -t "${img}" --load .
cname="smoke-${proj}"
docker rm -f "${cname}" >/dev/null 2>&1 || true
echo ">> running ${img}"
# Minimal env so Settings() constructs; sqlite avoids needing postgres.
docker run -d --name "${cname}" -p "${port}:${port}" \
  -e RASK_VIEWER_INPUT=s3://images-batch \
  -e RASK_VIEWER_OUTPUT=s3://images-batch-alto \
  -e RASK_ORCHESTRATOR_AUTOSTART=false \
  "${img}" >/dev/null
trap 'docker rm -f "${cname}" >/dev/null 2>&1 || true' EXIT
echo ">> waiting for health"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${port}${healthpath}" >/dev/null 2>&1; then
    echo "OK ${proj} healthy"; exit 0
  fi
  sleep 2
done
echo "FAIL ${proj} never became healthy"; docker logs "${cname}" | tail -30; exit 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sg docker -c 'bash .docker/smoke-build.sh core-api 8801'`
Expected: FAIL — `.docker/core-api.dockerfile` does not exist (buildx error "failed to read dockerfile").

- [ ] **Step 3: Create all six Dockerfiles**

Produce each file by substituting the template with its row from the table. (The files are mechanical substitutions — generate all six.)

- [ ] **Step 4: Delete the stale viewer.dockerfile**

```bash
git rm .docker/viewer.dockerfile
```

- [ ] **Step 5: Run the smoke test for each service**

Run (in the `docker` group, e.g. via `sg docker -c '…'`):
```bash
bash .docker/smoke-build.sh gateway 8888 /api/v1/docs || exit 1
for s in "core-api 8801" "search-api 8802" "volumes-api 8803" "ray-api 8804" "orchestrator 8810"; do
  bash .docker/smoke-build.sh $s || exit 1
done
```
Expected: `OK <svc> healthy` for all six. If a service needs an env var beyond the three provided to construct `Settings()`, add it to `smoke-build.sh` and note it for Task 3's compose env.

- [ ] **Step 6: Commit**

```bash
git add .docker/*.dockerfile .docker/smoke-build.sh
git rm --cached .docker/viewer.dockerfile 2>/dev/null || true
git commit -m "feat(docker): per-service Dockerfiles for the fleet; retire viewer image"
```

---

### Task 2: GPU ray-head image (arm64 CUDA 13)

Rebase the runner image onto an arm64 CUDA-13 base and extend it to run the Ray head and self-deploy the htrflow Serve app. This carries the accepted "assume base" GPU risk — verify torch sees the GPU inside the container.

**Files:**
- Create: `.docker/ray.dockerfile` (derived from `.docker/runner.dockerfile`)
- Keep: `.docker/runner.dockerfile` unchanged (still used elsewhere / x86)

**Interfaces:**
- Produces: image `ray:dev` that (a) `python -c "import torch; assert torch.cuda.is_available()"` passes under `--gpus all`, and (b) contains the `runner` entrypoint, `deploy_serve.py`, and htrflow deps so the container can run `ray start --head` and `deploy_serve.py up --app htrflow`.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write the GPU smoke test (failing test)**

Create `.docker/smoke-gpu.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
docker buildx build -f .docker/ray.dockerfile -t ray:dev --load .
echo ">> torch.cuda check inside container"
docker run --rm --gpus all ray:dev \
  python -c "import torch; print('cuda', torch.cuda.is_available()); assert torch.cuda.is_available()"
echo ">> runner entrypoint present"
docker run --rm ray:dev runner --help >/dev/null
echo "OK ray:dev GPU + runner"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sg docker -c 'bash .docker/smoke-gpu.sh'`
Expected: FAIL — `.docker/ray.dockerfile` does not exist yet.

- [ ] **Step 3: Create ray.dockerfile**

Copy `.docker/runner.dockerfile` to `.docker/ray.dockerfile` and make exactly these changes:

1. Both `FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04@sha256:…` lines → `FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04` (no digest pin initially — pin after first successful pull resolves a digest; `nvidia/cuda:13.0.1-base-ubuntu24.04` is confirmed to pull on this arm64 host).
2. Change `--package runner` → `--package runner` (unchanged — htrflow + serve live in the `runner` project).
3. In the final stage, after the venv copy, add the serve-deploy assets:
   ```dockerfile
   COPY components/scripts/deploy_serve.py /app/deploy_serve.py
   ```
   (htrflow pipeline config travels with the `runner`/`htr` packages already in the venv; if `deploy_serve.py` references a YAML by repo-relative path, also `COPY` that file and set its path via env — verify at build time.)
4. Update the `LABEL ... image.title` to `rask-ray` and description to `rask ray head + htrflow Serve`.
5. Keep `HF_HOME=/cache/hf`, the shm/ulimit-oriented ENV, and `ENTRYPOINT ["/usr/bin/tini","--"]`. Change default `CMD` to a no-op shell so compose supplies the command: `CMD ["bash","-lc","sleep infinity"]` (compose overrides with the real `ray start` command in Task 3).

- [ ] **Step 4: Run the GPU smoke test**

Run: `sg docker -c 'bash .docker/smoke-gpu.sh'`
Expected: `cuda True` then `OK ray:dev GPU + runner`.

**If `torch.cuda.is_available()` is False** (the accepted risk materialized): record it in the SDD ledger, and proceed with the CPU fallback — the image still works for CPU htrflow. Document in the spec's verification section that `RASK_SERVE_GPU_FRAC=0` + dropping `gpus: all` is required. Do not block the remaining tasks.

- [ ] **Step 5: Commit**

```bash
git add .docker/ray.dockerfile .docker/smoke-gpu.sh
git commit -m "feat(docker): arm64 CUDA-13 ray-head image with htrflow serve-deploy"
```

---

### Task 3: docker-compose.yml + .env.example

Wire infrastructure, the fleet, and the one-shot jobs into a single compose file with healthcheck-gated ordering.

**Files:**
- Create: `docker-compose.yml` (repo root)
- Create: `.env.example` (repo root; documents the compose env, gitignored real `.env`)

**Interfaces:**
- Consumes: images `gateway:dev`, `core-api:dev`, `search-api:dev`, `volumes-api:dev`, `ray-api:dev`, `orchestrator:dev` (Task 1), `ray:dev` (Task 2), existing `frontend` image (`.docker/frontend.dockerfile`).
- Produces: a running stack; gateway published on host `:8888`.

- [ ] **Step 1: Write the compose-validity test (failing test)**

Create `.docker/smoke-compose.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
echo ">> docker compose config validates"
docker compose --env-file .env.example config >/dev/null
echo "OK compose config valid"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `sg docker -c 'bash .docker/smoke-compose.sh'`
Expected: FAIL — no `docker-compose.yml` / `.env.example`.

- [ ] **Step 3: Write `.env.example`**

```dotenv
# MinIO / S3
AWS_ACCESS_KEY_ID=rask
AWS_SECRET_ACCESS_KEY=raskrask123
AWS_REGION=us-east-1
HCP_ENDPOINT=http://minio:9000
HCP_INSECURE=true
# Postgres
POSTGRES_USER=rask
POSTGRES_PASSWORD=rask
POSTGRES_DB=rask
DATABASE_URL=postgresql+asyncpg://rask:rask@postgres:5432/rask
# Ray
RAY_ADDRESS=ray://ray-head:10001
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0
# Phase-1 / pipeline flags
RASK_SOURCE_MODE=s3
RASK_VIEWER_INPUT=s3://images-batch
RASK_VIEWER_OUTPUT=s3://images-batch-alto
RASK_HTR_PIPELINE=htrflow
RASK_PREFETCH_PIPELINE=none
RASK_SERVE_REPLICAS=1
RASK_SERVE_GPU_FRAC=1.0
RASK_IIIF_URL=
# Gateway upstreams (compose DNS)
RASK_CORE_API_URL=http://core-api:8801
RASK_SEARCH_API_URL=http://search-api:8802
RASK_VOLUMES_API_URL=http://volumes-api:8803
RASK_RAY_API_URL=http://ray-api:8804
RASK_ORCH_API_URL=http://orchestrator:8810
```

- [ ] **Step 4: Write `docker-compose.yml`**

```yaml
name: rask

x-svc-env: &svc-env
  env_file: [.env]

x-healthcheck-api: &hc-api
  interval: 15s
  timeout: 3s
  retries: 10
  start_period: 20s

services:
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: ${AWS_ACCESS_KEY_ID}
      MINIO_ROOT_PASSWORD: ${AWS_SECRET_ACCESS_KEY}
    volumes: [minio-data:/data]
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 3s
      retries: 15
    ports: ["9001:9001"]   # console (optional)

  minio-init:
    image: minio/mc:latest
    depends_on:
      minio: {condition: service_healthy}
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 ${AWS_ACCESS_KEY_ID} ${AWS_SECRET_ACCESS_KEY} &&
      mc mb -p local/images-batch local/images-batch-alto || true"
    restart: "no"

  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes: [pg-data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 3s
      retries: 15

  migrate:
    image: core-api:dev
    pull_policy: never
    <<: *svc-env
    depends_on:
      postgres: {condition: service_healthy}
    working_dir: /app/components/services/core
    entrypoint: ["alembic", "upgrade", "head"]
    restart: "no"

  ray-head:
    image: ray:dev
    pull_policy: never
    <<: *svc-env
    command: >
      bash -lc "ray start --head --dashboard-host 0.0.0.0 --num-gpus=1 &&
      python /app/deploy_serve.py up --app htrflow &&
      tail -f /dev/null"
    gpus: all
    runtime: nvidia
    shm_size: "8gb"
    ulimits: {nofile: 65535}
    volumes: [hf-cache:/cache/hf]
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/htrflow >/dev/null 2>&1 || curl -fsS http://127.0.0.1:8265/api/version >/dev/null"]
      interval: 20s
      timeout: 5s
      retries: 30
      start_period: 120s

  core-api:
    image: core-api:dev
    pull_policy: never
    <<: *svc-env
    environment: {RASK_ORCHESTRATOR_AUTOSTART: "false"}
    depends_on:
      postgres: {condition: service_healthy}
      minio-init: {condition: service_completed_successfully}
      migrate: {condition: service_completed_successfully}
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8801/api/v1/health"], <<: *hc-api}

  search-api:
    image: search-api:dev
    pull_policy: never
    <<: *svc-env
    depends_on: {postgres: {condition: service_healthy}}
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8802/api/v1/health"], <<: *hc-api}

  volumes-api:
    image: volumes-api:dev
    pull_policy: never
    <<: *svc-env
    depends_on: {minio-init: {condition: service_completed_successfully}}
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8803/api/v1/health"], <<: *hc-api}

  ray-api:
    image: ray-api:dev
    pull_policy: never
    <<: *svc-env
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8804/api/v1/health"], <<: *hc-api}

  orchestrator:
    image: orchestrator:dev
    pull_policy: never
    <<: *svc-env
    environment: {RASK_ORCHESTRATOR_AUTOSTART: "false"}
    depends_on:
      postgres: {condition: service_healthy}
      migrate: {condition: service_completed_successfully}
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8810/api/v1/health"], <<: *hc-api}

  gateway:
    image: gateway:dev
    pull_policy: never
    <<: *svc-env
    depends_on:
      core-api: {condition: service_healthy}
      search-api: {condition: service_healthy}
      volumes-api: {condition: service_healthy}
      ray-api: {condition: service_healthy}
      orchestrator: {condition: service_healthy}
    ports: ["8888:8888"]
    healthcheck: {test: ["CMD","curl","-fsS","http://127.0.0.1:8888/api/v1/docs"], <<: *hc-api}

  frontend:
    build:
      context: .
      dockerfile: .docker/frontend.dockerfile
    image: frontend:dev
    depends_on: {gateway: {condition: service_healthy}}
    ports: ["8080:80"]

volumes:
  minio-data:
  pg-data:
  hf-cache:
```

> Notes for the implementer: (a) `gpus: all` requires Compose v2 with the NVIDIA runtime (already configured). If `gpus:` is unsupported by the installed compose, use the `deploy.resources.reservations.devices` form instead. (b) The `ray-head` healthcheck needs `curl` in `ray:dev` — confirm it is installed (add `curl` to the apt line in `ray.dockerfile` if missing). (c) `migrate` `working_dir` must be where `alembic.ini` lives — verify the path `components/services/core` contains `alembic.ini`; adjust if alembic is rooted elsewhere.

- [ ] **Step 5: Validate compose config**

Run: `sg docker -c 'cp .env.example .env && bash .docker/smoke-compose.sh'`
Expected: `OK compose config valid`.

- [ ] **Step 6: Ensure `.env` is gitignored**

```bash
grep -qxF '.env' .gitignore || echo '.env' >> .gitignore
```

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml .env.example .docker/smoke-compose.sh .gitignore
git commit -m "feat(docker): compose stack — infra, fleet, one-shot migrate/bucket jobs"
```

---

### Task 4: Makefile targets

Add the `compose-*` runbook targets so the stack builds and boots with one command, alongside the existing Makefile.

**Files:**
- Modify: `Makefile` (append a "Docker Compose" section)

**Interfaces:**
- Consumes: `docker-compose.yml`, the Dockerfiles, `.env.example`.
- Produces: `make compose-build`, `make compose-up`, `make compose-down`, `make compose-logs`, `make compose-purge`.

- [ ] **Step 1: Add targets to the Makefile**

Append:

```makefile
# ---- Docker Compose (full local stack) -------------------------------------
DC ?= docker compose
COMPOSE_IMAGES = gateway core-api search-api volumes-api ray-api orchestrator

compose-env:
	@test -f .env || cp .env.example .env

compose-build: ## Build all fleet images (+ ray) on native arm64
	@for s in $(COMPOSE_IMAGES); do \
	  echo ">> building $$s:dev"; \
	  docker buildx build -f .docker/$$s.dockerfile -t $$s:dev --load . || exit 1; \
	done
	docker buildx build -f .docker/ray.dockerfile -t ray:dev --load .

compose-up: compose-env ## Bring up the whole stack and wait for health
	$(DC) up -d --wait
	@echo "gateway → http://localhost:8888   minio console → http://localhost:9001"

compose-down: ## Stop the stack (keep volumes)
	$(DC) down

compose-purge: ## Stop the stack and delete data volumes
	$(DC) down --volumes

compose-logs: ## Tail all service logs
	$(DC) logs -f --tail=100
```

- [ ] **Step 2: Verify the targets resolve**

Run: `make -n compose-up`
Expected: prints the `docker compose up -d --wait` command (dry-run, no execution).

- [ ] **Step 3: Build all images via the target**

Run: `sg docker -c 'make compose-build'`
Expected: each `>> building <svc>:dev` succeeds; `ray:dev` builds (or the documented GPU/CPU note from Task 2 applies).

- [ ] **Step 4: Commit**

```bash
git add Makefile
git commit -m "feat(make): compose-build/up/down/logs/purge targets"
```

---

### Task 5: End-to-end verification + CPU fallback note

Bring the whole stack up and prove the Phase-1 round-trip (upload → register → htrflow → ALTO) through the live compose stack. Document the GPU/CPU outcome.

**Files:**
- Modify: `docs/superpowers/specs/2026-06-18-rask-docker-compose-design.md` (fill the verification section with the observed result + CPU fallback if needed)

**Interfaces:**
- Consumes: the full running stack (`make compose-up`).

- [ ] **Step 1: Bring up the stack**

Run: `sg docker -c 'make compose-up'`
Expected: command returns after `--wait`; `docker compose ps` shows all long-running services `healthy`, and `migrate` + `minio-init` `exited (0)`.

- [ ] **Step 2: Upload two sample images to the input bucket**

```bash
# sample images: any two .jpg pages (reuse a known test pair)
sg docker -c 'docker run --rm --network rask_default -v "$PWD/samples:/s" \
  --entrypoint sh minio/mc:latest -c "\
  mc alias set local http://minio:9000 rask raskrask123 && \
  mc cp /s/page_0001.jpg /s/page_0002.jpg local/images-batch/testvol/"'
```
Expected: `mc cp` reports 2 objects copied. (Adjust the network name to match `docker compose ps` / `docker network ls` — compose default is `rask_default`.)

- [ ] **Step 3: Register the volume via the gateway**

Run: `curl -fsS -X POST http://localhost:8888/api/v1/batches/testvol/register`
Expected: HTTP 201 JSON with `"page_count": 2`, `"manifest_status": "ok"`, `"htr_status": "cached"`.

- [ ] **Step 4: Trigger processing and wait for ALTO**

```bash
curl -fsS -X POST http://localhost:8888/api/v1/orchestrator/start
# poll the output bucket for ALTO
for i in $(seq 1 60); do
  n=$(sg docker -c 'docker run --rm --network rask_default --entrypoint sh minio/mc:latest -c "mc alias set local http://minio:9000 rask raskrask123 >/dev/null && mc ls --recursive local/images-batch-alto/testvol/ 2>/dev/null | wc -l"')
  echo "alto files: $n"; [ "$n" -ge 2 ] && break; sleep 10
done
```
Expected: eventually `alto files: 2` (two `.xml` objects). If the orchestrator path is slow/uncertain, the fallback is running the runner directly inside `ray-head` (documented in Step 6).

- [ ] **Step 5: Tear down**

Run: `sg docker -c 'make compose-down'`
Expected: stack stops; volumes retained.

- [ ] **Step 6: Record the result in the spec**

Fill the design doc's "Testing / verification" section with: the observed `compose ps` health, the `register` 201 with `page_count=2`, the ALTO count, and whether the GPU path worked (`torch.cuda.is_available()` from Task 2). If GPU failed, add the CPU-fallback recipe: set `RASK_SERVE_GPU_FRAC=0` in `.env` and remove `gpus: all`/`runtime: nvidia` from `ray-head`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-06-18-rask-docker-compose-design.md
git commit -m "docs: record compose end-to-end verification result"
```

---

## Self-Review

**Spec coverage:**
- Topology table (8 containers + 2 one-shots) → Tasks 1/2/3 ✓
- Per-service images on shared base → Task 1 ✓
- GPU ray image (arm64 cu13, assume base) → Task 2 ✓
- Config & wiring (DATABASE_URL, HCP_ENDPOINT, RAY_ADDRESS, gateway upstreams, Phase-1 flags) → Task 3 `.env.example` ✓
- GPU/volumes/ordering (gpus all, shm, named volumes, healthcheck depends_on) → Task 3 ✓
- Make targets → Task 4 ✓
- E2E round-trip + CPU fallback doc → Task 5 ✓
- Out-of-scope (k3s/NATS/multi-volume) → not implemented, correct ✓

**Placeholder scan:** Dockerfile template uses explicit `__TOKEN__` markers with a full substitution table (mechanical, not vague). Compose/env/Makefile shown in full. The only deliberately deferred items are runtime-discovered details flagged with "verify/adjust" notes (alembic path, gpus syntax, network name, curl presence) — these are genuine environment confirmations, not skipped work.

**Type/name consistency:** image tags (`<svc>:dev`, `ray:dev`), ports, modules, env var names, health path `/api/v1/health`, bucket names, and the `register`/`orchestrator/start` endpoints are identical across the spec, Task 1 template, Task 3 compose, and Task 5 verification. Gateway upstream vars match the grepped source (`RASK_*_API_URL`).
