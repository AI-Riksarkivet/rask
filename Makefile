.PHONY: help install build test lint fmt clean storybook typecheck check ci viewer dev-micro dev-frontends viewer-frontend frontend-storage frontend-compute frontend-build frontend-check ray-up ray-down ray-status serve-up serve-down serve-status search-index search-index-fresh harvest-ead catalog-index pg-up pg-down pg-status pg-deps pg-migrate pg-revision claude-bootstrap ray-up-htr serve-up-both qwen-serve compose-env compose-build compose-up compose-down compose-purge compose-logs

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck check ci   frontend-check frontend-build"
	@echo "  viewer                                 — core monolith dev server (:8888)"
	@echo "  dev-micro                              — backend fleet (gateway :8888 + per-domain services)"
	@echo "  dev-frontends                          — all microfrontends (:5173 + :5174/storage + :5175/compute)"
	@echo "  viewer-frontend frontend-storage frontend-compute   — run one app each"
	@echo "  ray-up ray-down ray-status   ray-up-htr (2-GPU pool, GPUs 0,1)"
	@echo "  serve-up serve-down serve-status   serve-up-both (transcribe+htrflow)"
	@echo "  qwen-serve                             — vLLM Qwen3.6-27B on GPU 2 for OpenCode"
	@echo "  search-index search-index-fresh harvest-ead catalog-index"
	@echo "  pg-up pg-down pg-status pg-migrate pg-revision MSG='...'"
	@echo "  claude-bootstrap                       — install Claude Code skills & verify config"

install:
	bun install
	uv sync

build:
	uv sync
	bun run build

# Python tests via pytest; the frontends have no unit suite — `make frontend-check`
# (svelte-check) is their gate.
test:
	uv run pytest

lint:
	uv run ruff check .
	bun run lint

fmt:
	uv run ruff format .
	bun run format

storybook:
	bun run storybook

clean:
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/.turbo **/storybook-static

# ---- python (uv workspace) -------------------------------------------------
typecheck:
	uvx ty check

check: fmt lint typecheck

ci: check test

# ---- claude code -----------------------------------------------------------
claude-bootstrap:
	@command -v claude  >/dev/null 2>&1 || { echo "  !! claude CLI not found — install Claude Code first"; exit 1; }
	@command -v bunx    >/dev/null 2>&1 || { echo "  !! bunx not found — install bun first (https://bun.sh)"; exit 1; }
	@command -v python3 >/dev/null 2>&1 || { echo "  !! python3 not found"; exit 1; }
	@echo "==> Svelte MCP server (local scope, project section of ~/.claude.json)..."
	@claude mcp add -t stdio -s local svelte -- bunx -y @sveltejs/mcp || echo "    (already installed — skipping)"
	@echo
	@echo "==> Adding marketplaces declared in .claude/settings.json (idempotent)..."
	@python3 -c 'import json; print("\n".join(v["source"]["repo"] for v in json.load(open(".claude/settings.json")).get("extraKnownMarketplaces",{}).values()))' \
		| while read -r repo; do echo "    + $$repo"; claude plugin marketplace add "$$repo" >/dev/null 2>&1 || true; done
	@echo
	@echo "==> Installing enabled plugins at project scope (idempotent)..."
	@python3 -c 'import json; print("\n".join(k for k,v in json.load(open(".claude/settings.json")).get("enabledPlugins",{}).items() if v))' \
		| while read -r plugin; do echo "    + $$plugin"; claude plugin install "$$plugin" -s project >/dev/null 2>&1 || true; done
	@echo
	@echo "==> Done — re-run anytime (idempotent). Skills come from the ra-skills marketplace; see .claude/README.md."
	@echo "    Authenticate any MCP servers if prompted on first use."

# ---- viewer ----------------------------------------------------------------
# Port must be 8888 — components/apps/frontend Vite proxy defaults
# VIEWER_BACKEND to http://localhost:8888.
VIEWER_INPUT  ?= s3://images-batch
VIEWER_OUTPUT ?= s3://images-batch-alto

viewer:
	RASK_VIEWER_INPUT=$(VIEWER_INPUT) RASK_VIEWER_OUTPUT=$(VIEWER_OUTPUT) \
		uv run uvicorn core.main:app --host 0.0.0.0 --port 8888 --reload

# Local microservice fleet (gateway + per-domain backends) via dev-micro.sh.
# Bring up deps first: `make ray-up`, `make pg-up` (+ `make pg-migrate`); S3/HCP
# from .env. The gateway listens on :8888 so the frontends' /api proxy works.
dev-micro:
	uv sync --all-packages
	./dev-micro.sh

# ---- frontends (SvelteKit microfrontends) ----------------------------------
# Three independent SvelteKit SSR apps (svelte-adapter-bun) + the @rask/ui
# watcher, orchestrated by Turborepo. Each app's Vite dev server proxies
# /api/* → VIEWER_BACKEND (the gateway / `make viewer`, :8888). The apps come up on
# their own ports AND Turborepo auto-starts its built-in microfrontends proxy (from
# components/apps/frontend/microfrontends.json — no extra package) on :3024:
#   single origin → http://localhost:3024   (browse THIS for cross-app nav)
#   viewer-frontend :5173 (catch-all) · storage :5174 /storage · compute :5175 /compute
# The shared @rask/ui shell + nav render with NO backend; start one
# (`make dev-micro` or `make viewer`) only when you need live /api data.
dev-frontends:        # all three apps + @rask/ui watcher + :3024 proxy (turbo run dev)
	bun run dev

viewer-frontend:      # catch-all app only, :5173
	bun run dev:frontend

frontend-storage:     # storage app only, :5174 /storage
	bun run dev:storage

frontend-compute:     # compute app only, :5175 /compute
	bun run dev:compute

frontend-build:       # production-build every app + @rask/ui (turbo, cached)
	bun run build

frontend-check:       # svelte-check every app + @rask/ui (turbo)
	bun run check

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

# Single CPU/1-GPU htrflow endpoint for the low-resource / local-k3s shape.
serve-up-htrflow:
	RASK_SERVE_REPLICAS=1 RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python components/scripts/deploy_serve.py up --app htrflow

# ---- GPU split: HTR on 2 GPUs, Qwen LLM on the 3rd -------------------------
# transcribe + htrflow co-reside on a 2-GPU Ray pool (GPUs 0,1) via fractional
# Serve reservations: 2 apps x RASK_SERVE_REPLICAS x RASK_SERVE_GPU_FRAC.
# Defaults: 2 x 2 x 0.49 = 1.96 GPU, leaving headroom for the htr pipeline's
# Layout/Line num_gpus=0.001 fractions. GPU 2 is reserved for qwen-serve.
HTR_CUDA_DEVICES    ?= 0,1
RASK_SERVE_REPLICAS ?= 2
RASK_SERVE_GPU_FRAC ?= 0.49

ray-up-htr:
	@if ray status >/dev/null 2>&1; then \
	  echo "Ray already running. ray-down first to re-pin the GPU pool."; \
	else \
	  CUDA_VISIBLE_DEVICES=$(HTR_CUDA_DEVICES) RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 \
	    uv run --no-sync ray start --head --port=$(RAY_HEAD_PORT) --num-gpus=2 \
	    --dashboard-host=0.0.0.0 --dashboard-port=$(RAY_DASHBOARD_PORT); \
	  echo "Ray (2-GPU HTR pool, devices $(HTR_CUDA_DEVICES)) dashboard: http://localhost:$(RAY_DASHBOARD_PORT)"; \
	fi

serve-up-both:
	RASK_SERVE_REPLICAS=$(RASK_SERVE_REPLICAS) RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python components/scripts/deploy_serve.py up --app transcribe
	RASK_SERVE_REPLICAS=$(RASK_SERVE_REPLICAS) RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python components/scripts/deploy_serve.py up --app htrflow

# ---- Qwen3.6-27B LLM backend for OpenCode (external, isolated venv) --------
# Lives outside the rask uv workspace: vLLM pins torch/transformers that clash
# with the HTR venv. Pinned to GPU 2 so it never contends with the HTR pool.
# Exposes an OpenAI-compatible API at http://localhost:$(QWEN_PORT)/v1.
QWEN_VENV        ?= $(HOME)/qwen-serve/.venv
QWEN_MODEL       ?= Qwen/Qwen3.6-27B
# Port 8001, not 8000: Ray Serve's HTTP proxy holds :8000 (unused by the HTR
# pipeline, which calls Serve via in-process handles, but it owns the port).
QWEN_PORT        ?= 8001
QWEN_CTX         ?= 131072
QWEN_CUDA_DEVICE ?= 2
# Gated-DeltaNet (Mamba-style) needs one state-cache block per concurrent
# sequence. The default 1024 exceeds what fits alongside 131K-token KV cache;
# a single-user OpenCode backend needs only a handful, so cap well under that.
QWEN_MAX_SEQS    ?= 256

# VLLM_USE_FLASHINFER_SAMPLER=0: this box has no CUDA toolkit (nvcc), so
# flashinfer's JIT-compiled sampler kernel can't build. Fall back to vLLM's
# native PyTorch top-k/top-p sampler (no compiler needed, negligible impact).
qwen-serve:
	CUDA_VISIBLE_DEVICES=$(QWEN_CUDA_DEVICE) VLLM_USE_FLASHINFER_SAMPLER=0 \
	  $(QWEN_VENV)/bin/vllm serve $(QWEN_MODEL) \
	  --port $(QWEN_PORT) --tensor-parallel-size 1 \
	  --max-model-len $(QWEN_CTX) --max-num-seqs $(QWEN_MAX_SEQS) --reasoning-parser qwen3

# ---- search / catalog index ------------------------------------------------
search-index:
	uv run python components/scripts/submit_index.py

search-index-fresh:
	uv run python components/scripts/submit_index.py --skip-existing

harvest-ead:
	uv run python components/scripts/harvest_ead.py

catalog-index:
	uv run python components/scripts/index_catalog.py --no-embed --digitized-only

# ---- local postgres (for alembic + viewer testing) -------------------------
# Local dev postgres in a docker container. Connect from the VS Code
# `ms-ossdata.vscode-pgsql` extension or any psql client at:
#   postgresql://rask:rask@localhost:5432/rask
PG_URL ?= postgresql+asyncpg://rask:rask@localhost:5432/rask

pg-up:
	docker run -d --name rask-pg \
	  -e POSTGRES_USER=rask -e POSTGRES_PASSWORD=rask -e POSTGRES_DB=rask \
	  -p 5432:5432 postgres:16
	@echo "Postgres up. Connect via:"
	@echo "  - VS Code (ms-ossdata.vscode-pgsql): host=localhost port=5432 db=rask user=rask password=rask"
	@echo "  - psql:    psql postgresql://rask:rask@localhost:5432/rask"
	@echo "  - alembic: make pg-migrate"

pg-down:
	docker rm -f rask-pg

pg-status:
	docker ps --filter name=rask-pg --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

pg-deps:
	uv sync --package core --extra postgres --extra migrations

pg-migrate: pg-deps
	cd components/services/core && \
	  DATABASE_URL=$(PG_URL) uv run --package core alembic upgrade head

pg-revision: pg-deps
	cd components/services/core && \
	  DATABASE_URL=$(PG_URL) uv run --package core alembic revision --autogenerate -m "$(MSG)"

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
