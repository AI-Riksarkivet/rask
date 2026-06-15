.PHONY: help install build test lint fmt clean storybook typecheck check ci viewer viewer-frontend viewer-frontend-build dev-micro ray-up ray-down ray-status serve-up serve-down serve-status search-index search-index-fresh harvest-ead catalog-index pg-up pg-down pg-status pg-deps pg-migrate pg-revision claude-bootstrap ray-up-htr serve-up-both qwen-serve

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck check ci"
	@echo "  viewer viewer-frontend viewer-frontend-build"
	@echo "  dev-micro                              — local microservice fleet (gateway + backends)"
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
	cargo build --workspace
	uv sync
	bun run build

test:
	cargo test --workspace
	uv run pytest
	bun run test

lint:
	cargo clippy --workspace -- -D warnings
	uv run ruff check .
	bun run lint

fmt:
	cargo fmt --all
	uv run ruff format .
	bun run format

storybook:
	bun run storybook

clean:
	cargo clean
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/storybook-static

# ---- python (uv workspace) -------------------------------------------------
typecheck:
	uvx ty check

check: fmt lint typecheck

ci: check test

# ---- claude code -----------------------------------------------------------
claude-bootstrap:
	@command -v claude >/dev/null 2>&1 || { echo "  !! claude CLI not found — install Claude Code first"; exit 1; }
	@command -v bunx   >/dev/null 2>&1 || { echo "  !! bunx not found — install bun first (https://bun.sh)"; exit 1; }
	@echo "==> Installing svelte MCP server (local scope, project-scoped section of ~/.claude.json)..."
	@claude mcp add -t stdio -s local svelte -- bunx -y @sveltejs/mcp || echo "    (already installed — skipping)"
	@echo
	@echo "==> Verifying Claude config..."
	@test -f .claude/settings.json && echo "    OK  .claude/settings.json" || echo "    !!  missing .claude/settings.json"
	@echo
	@echo "==> Manual steps that can't be scripted (see .claude/README.md):"
	@echo "    1. In Claude Code: /plugin marketplace add (and /plugin install) entries from README."
	@echo "    2. Authenticate any MCP servers if prompted on first use."

# ---- viewer ----------------------------------------------------------------
# Port must be 8888 — components/apps/frontend Vite proxy defaults
# VIEWER_BACKEND to http://localhost:8888.
VIEWER_INPUT  ?= s3://images-batch
VIEWER_OUTPUT ?= s3://images-batch-alto

viewer:
	RASK_VIEWER_INPUT=$(VIEWER_INPUT) RASK_VIEWER_OUTPUT=$(VIEWER_OUTPUT) \
		uv run uvicorn viewer.main:app --host 0.0.0.0 --port 8888 --reload

viewer-frontend:
	bun --cwd components/apps/frontend run dev

# Local microservice fleet (gateway + per-domain backends) via dev-micro.sh.
# Bring up deps first: `make ray-up`, `make pg-up` (+ `make pg-migrate`); S3/HCP
# from .env. The gateway listens on :8888 so `make viewer-frontend` works as-is.
dev-micro:
	uv sync --all-packages
	./dev-micro.sh

viewer-frontend-build:
	bun --cwd components/apps/frontend run build

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
	uv sync --package viewer --extra postgres --extra migrations

pg-migrate: pg-deps
	cd components/services/viewer && \
	  DATABASE_URL=$(PG_URL) uv run --package viewer alembic upgrade head

pg-revision: pg-deps
	cd components/services/viewer && \
	  DATABASE_URL=$(PG_URL) uv run --package viewer alembic revision --autogenerate -m "$(MSG)"
