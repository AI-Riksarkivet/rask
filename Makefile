.PHONY: help install build test lint fmt clean storybook typecheck check ci viewer viewer-frontend viewer-frontend-build ray-up ray-down ray-status serve-up serve-down serve-status search-index search-index-fresh harvest-ead catalog-index

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck check ci"
	@echo "  viewer viewer-frontend viewer-frontend-build"
	@echo "  ray-up ray-down ray-status"
	@echo "  serve-up serve-down serve-status"
	@echo "  search-index search-index-fresh harvest-ead catalog-index"

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
	bun run --filter '*' format

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
