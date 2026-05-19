# ──────────────────────────────────────────────────────────────────────
#  HCP App — Development commands
# ──────────────────────────────────────────────────────────────────────
SHELL := /bin/bash
export PATH := $(HOME)/.deno/bin:$(PATH)

.PHONY: help setup install-deno install-uv setup-hooks skills \
        fmt lint quality \
        run-api run-api-mock \
        frontend-dev frontend-build storybook build-storybook test-storybook \
        docs docs-build \
        checks test test-integration serve-backend full-serve build \
        publish publish-backend publish-frontend

## help: list available targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  make /' | sed 's/: /\t/'

# ── Setup ────────────────────────────────────────────────────────────

## setup: install all tooling, dependencies, and git hooks
setup: install-deno install-uv setup-hooks
	cd backend && uv sync --extra serve --extra lance
	cd frontend && deno install
	@echo ""
	@echo "For Claude Code skills, see: .claude/README.md"

## install-deno: install Deno runtime
install-deno:
	@command -v deno >/dev/null 2>&1 || curl -fsSL https://deno.land/install.sh | sh

## install-uv: install uv (Python package manager)
install-uv:
	@command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh

## setup-hooks: install git pre-commit hook
setup-hooks:
	ln -sf ../../.claude/hooks/pre-commit .git/hooks/pre-commit
	@echo "Git pre-commit hook installed."

## skills: show Claude Code skill install guide
skills:
	@cat .claude/README.md

# ── Quality ──────────────────────────────────────────────────────────

## fmt: format all code (backend + frontend)
fmt:
	cd backend && uvx ruff format .
	cd frontend && deno task fmt

## lint: lint all code (backend + frontend)
lint:
	cd backend && uvx ruff check .
	cd frontend && deno lint

## quality: format, lint, and type-check everything
quality: fmt lint
	cd frontend && deno task check || echo "Warning: frontend type errors (see above)"
	cd backend && uvx ty check || echo "Warning: backend type errors (see above)"

# ── API ──────────────────────────────────────────────────────────────

## run-api: start the HCP API server (with Lance support)
ROOT_PATH ?=
run-api:
	cd backend && ROOT_PATH=$(ROOT_PATH) uv run --extra serve --extra lance uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 $(if $(ROOT_PATH),--root-path $(ROOT_PATH))

## run-api-mock: start mock dev server (login: admin/password)
run-api-mock:
	cd backend && uv run --extra serve uvicorn mock_server:app --reload --host 0.0.0.0 --port 8000

# ── Frontend ─────────────────────────────────────────────────────────

## frontend-dev: start the frontend dev server
frontend-dev:
	cd frontend && BACKEND_URL=http://127.0.0.1:8000 deno task dev

## frontend-build: build the frontend for production
frontend-build:
	cd frontend && deno task build

## storybook: start Storybook dev server on port 6006
storybook:
	cd frontend && deno task storybook

## build-storybook: build Storybook static site
build-storybook:
	cd frontend && deno task build-storybook

## test-storybook: run Storybook interaction + a11y tests via Vitest
test-storybook:
	cd frontend && deno task test-storybook

# ── Docs ─────────────────────────────────────────────────────────────

## docs: serve documentation site locally (live-reload)
docs:
	PYTHONPATH=backend uvx --with mkdocstrings-python zensical serve

## docs-build: build documentation site for deployment
docs-build:
	PYTHONPATH=backend uvx --with mkdocstrings-python zensical build --clean

# ── Dagger ───────────────────────────────────────────────────────────

## checks: run all lint & type-checks in Dagger
checks:
	dagger call checks --source=.

## test: run unit tests with coverage in Dagger
test:
	dagger call test-coverage --source=.

## test-integration: run integration tests (real Redis) in Dagger
test-integration:
	dagger call test-integration --source=.

## serve-backend: start backend + Redis in Dagger on :8000
serve-backend:
	dagger call serve --source=. up --ports 8000:8000

## full-serve: start full stack in Dagger — frontend on :5174, backend internal
full-serve:
	dagger call serve-all --source=. up --ports 5174:8000

## build: build backend and frontend containers in Dagger
build:
	dagger call build-backend --source=.
	dagger call build-frontend --source=.

# ── Publish ──────────────────────────────────────────────────────────
TAG ?= v0.1.0

## publish: publish both backend and frontend images to Docker Hub
publish:
	@DOCKER_USERNAME=$$(grep '^DOCKER_USERNAME=' .env | cut -d= -f2 | tr -d '"') \
	DOCKER_PASSWORD=$$(grep '^DOCKER_PASSWORD=' .env | cut -d= -f2 | tr -d '"') \
	dagger call publish-all --source=. --tag=$(TAG) \
		--docker-username=env:DOCKER_USERNAME \
		--docker-password=env:DOCKER_PASSWORD

## publish-backend: publish backend image to Docker Hub
publish-backend:
	@DOCKER_USERNAME=$$(grep '^DOCKER_USERNAME=' .env | cut -d= -f2 | tr -d '"') \
	DOCKER_PASSWORD=$$(grep '^DOCKER_PASSWORD=' .env | cut -d= -f2 | tr -d '"') \
	dagger call publish-backend --source=. --tag=$(TAG) \
		--docker-username=env:DOCKER_USERNAME \
		--docker-password=env:DOCKER_PASSWORD

## publish-frontend: publish frontend image to Docker Hub
publish-frontend:
	@DOCKER_USERNAME=$$(grep '^DOCKER_USERNAME=' .env | cut -d= -f2 | tr -d '"') \
	DOCKER_PASSWORD=$$(grep '^DOCKER_PASSWORD=' .env | cut -d= -f2 | tr -d '"') \
	dagger call publish-frontend --source=. --tag=$(TAG) \
		--docker-username=env:DOCKER_USERNAME \
		--docker-password=env:DOCKER_PASSWORD
