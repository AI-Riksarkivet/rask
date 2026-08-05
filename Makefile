.PHONY: registry-gc dagger-gc dev-gc help install build test test-slow lint fmt clean storybook typecheck knip check ci dev-micro dev-frontends dev-frontends-k3s home frontend-build frontend-check sync-favicons ray-up ray-down ray-status serve-up serve-down serve-status harvest-ead claude-bootstrap ray-up-htr serve-up-both qwen-serve k3s-install k3s-deps k3s-build k3s-import k3s-up k3s-down k3s-purge k9s bootstrap dev-registry e2e frontend-images prod-render-check alert-rules-check audit scan-config scan-image scan-zone-image

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck knip check ci   frontend-check frontend-build"
	@echo "  dev-micro                              — backend fleet (gateway :8888 + per-domain services)"
	@echo "  dev-frontends                          — all 6 zones behind the :3024 proxy (browse http://localhost:3024)"
	@echo "  dev-frontends-k3s                      — same, but /api → the IN-CLUSTER gateway (port-forwarded)"
	@echo "  home frontend-<zone>                   — run one zone each (e.g. frontend-explorer)"
	@echo "  dev-gc                                 — reclaim the dev registry + the Dagger cache"
	@echo "  ray-up ray-down ray-status   ray-up-htr (2-GPU pool, GPUs 0,1)"
	@echo "  serve-up serve-down serve-status   serve-up-both (transcribe+htrflow)"
	@echo "  qwen-serve                             — vLLM Qwen3.6-27B on GPU 2 for OpenCode"
	@echo "  harvest-ead"
	@echo "  claude-bootstrap                       — install Claude Code skills & verify config"

# --all-packages, not a bare `uv sync`: a bare sync installs the ROOT project only and
# omits every workspace member, so a fresh clone gets a venv where `import service_kit`
# fails. Same flag as .dagger and `make dev-micro` — one behaviour everywhere.
install:
	bun --cwd=frontend install
	uv sync --all-packages

build:
	uv sync --all-packages
	bun --cwd=frontend run build

# Python tests via pytest; the frontends have no unit suite — `make frontend-check`
# (svelte-check) is their gate.
# `not e2e`: tests/e2e-py is collectable (so the collection gate in
# tests/unit/test_e2e_collection_gate.py can see it) but its suites need a LIVE deployed
# stack — run them via `make e2e-ci` / `make e2e-ray-ci` / the per-suite targets.
test:
	uv run pytest -m "not slow and not e2e"
	# The HTR runner is sealed OUT of the root workspace (own lock, own venv): the root
	# pytest can neither import nor collect its tests, so without this second line the
	# runner suite silently never runs. cd first — from the repo root, pytest would read
	# the ROOT testpaths and try to import fleet modules absent from the runner's venv.
	cd runners/htr && uv run --frozen pytest -m "not slow"
	cd runners/dummy && uv run --frozen pytest

# Slow tests need real models / a GPU (e.g. the YOLO layout smoke test) and hang on
# hosts without them — opt in explicitly. Runs the full suite including slow marks.
test-slow:
	uv run pytest -m "not e2e"
	# The HTR runner is sealed OUT of the root workspace (its model stack must not enter the
	# fleet's resolution), so the root pytest cannot see it. Run its suite in its own env —
	# without this line 28 tests would silently never run.
	uv run --project runners/htr --frozen pytest

lint:
	uv run ruff check .
	bun --cwd=frontend run lint

fmt:
	uv run ruff format .
	bun --cwd=frontend run fmt

storybook:
	bun --cwd=frontend run storybook

clean:
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/.turbo **/storybook-static

# ---- python (uv workspace) -------------------------------------------------
typecheck:
	uvx ty check

# ---- OpenAPI contract (docs/*-openapi.json) --------------------------------
# The committed specs ARE the reviewable contract, generated from the live FastAPI
# apps. CI enforces drift via `dagger call openapi` (.dagger/openapi.go, ci.yml) —
# these targets are the same gate, reachable locally. Both scripts/gen_openapi.py
# and .dagger/openapi.go referenced them by name for months before they existed.
# `--all-packages` is load-bearing: the root pyproject has `dependencies = []`, so a plain
# `uv run` installs no workspace member and `import catalog.main` fails. CI gets this via
# .dagger/test.go's `uv sync --all-packages`; locally we do the same.
openapi:
	uv sync --all-packages
	uv run --no-sync scripts/gen_openapi.py

# Diffs against HEAD, not the index: `git diff` alone compares to the index, so a *staged*
# drifted spec would pass falsely. CI's dagger function snapshots the committed files and
# diffs the regenerated output back, which is what `git diff HEAD` reproduces.
openapi-check: openapi
	@git diff HEAD --exit-code -- docs/catalog-openapi.json docs/lineage-openapi.json \
	  || { echo "!! OpenAPI drift: a route changed without refreshing the spec. Commit the diff above."; exit 1; }
	@echo "OpenAPI specs match the committed contract"

# ---- zone images -----------------------------------------------------------
# `make frontend-images` == the ci.yml "Build every zone image" step, byte-for-byte — the same
# contract `dagger call charts` == `make charts` holds. The step invoked this target by name while it
# did not exist, so the web-smoke job died with make's exit 2 ("No rule to make target") before
# building anything: the gate's whole claim ("the dockerfile still builds") had never been tested.
#
# ZONES is `?=`, so ci.yml's scan of microfrontends/*/package.json wins when exported; locally the
# committed list applies. `|| exit 1` stops at the FIRST failing zone rather than reporting the last
# one's status for all of them.
#
# The build itself goes through DAGGER (scripts/dagger-image.sh -> `dagger call zone-image`), like every
# other image in this repo since 2026-07-29. The dockerfile is unchanged and still declares the OCI
# provenance args (BUILD_DATE / VCS_REF / VERSION); the helper supplies them, because a Dagger Function
# is sandboxed and cannot read git — and stamping a timestamp inside the module would change the build
# args on every invocation and defeat the layer cache.
FRONTEND_TAG ?= dev

frontend-images: ## Build every zone image from the one parametrized dockerfile (web-<zone>:$(FRONTEND_TAG))
	@for a in $(ZONES); do \
	  VERSION="$(FRONTEND_TAG)" bash scripts/dagger-image.sh --zone $$a --tag web-$$a:$(FRONTEND_TAG) || exit 1; \
	done

# ---- chart gates (prod overlay + alert rules) -------------------------------
# CI enforces both via `dagger call charts` (.dagger/charts.go), which shells out to these two
# targets BY NAME — and neither existed, so `dagger call charts` died at the first of them and the
# prod-overlay HA/security invariants and the alert-rule proofs had never once run. Exactly the
# defect the OpenAPI note above records ("referenced them by name for months before they existed"),
# repeated one section down; and both assets already specify their own commands — scripts/
# prod_render_check.sh's header says "Run: `make prod-render-check`", and chart/alerting/
# rules_test.yml's says this target runs check-then-test. Only the wrappers were missing.
#
# Bare `helm`/`promtool` on purpose: the Dagger container curls both to /usr/local/bin and
# deliberately excludes .localbin so a host-downloaded binary cannot shadow the pinned one.
prod-render-check: ## Render values-prod.yaml and assert its HA + security switches are ON
	./scripts/prod_render_check.sh

alert-rules-check: ## promtool: the alert rules are valid AND actually fire on synthetic series
	promtool check rules chart/alerting/rules.yml
	promtool test rules chart/alerting/rules_test.yml

# ---- frontend dead-code + dep gate (knip, repo-wide; see knip.json) ---------
# Cross-workspace tool — analyses the whole JS graph at once, so it stays a
# root-level gate, not a per-package turbo task (lint/fmt ARE per-package turbo tasks).
knip:
	bun --cwd=frontend run knip

check: fmt lint typecheck knip

ci: check test

# ---- supply chain (.dagger/scan.go) ----------------------------------------
# Same contract as every other gate here: `make audit` == `dagger call audit`, one definition, no
# second description of it. Deliberately NOT folded into `check` — check is offline and fast, these
# reach osv.dev and ghcr for advisory data, and a laptop on a train should not fail `make check`.
#
# audit       osv-scanner over all SIX lockfiles + .dagger/go.mod. No build, seconds.
# scan-config trivy misconfig over .docker/ + chart/, and secret detection repo-wide. No build.
# scan-image  trivy over an image DAGGER BUILT — needs the build first, so it is minutes, not seconds.
#             NAME is the .docker/<stem>.dockerfile stem; ZONE is a frontend/microfrontends/ dir.
audit:
	dagger call audit

scan-config:
	dagger call scan-config

scan-image:
	@test -n "$(NAME)" || { echo "  !! usage: make scan-image NAME=gateway"; exit 1; }
	dagger call scan-image --name=$(NAME)

scan-zone-image:
	@test -n "$(ZONE)" || { echo "  !! usage: make scan-zone-image ZONE=home"; exit 1; }
	dagger call scan-zone-image --zone=$(ZONE)

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

# ---- backend fleet ---------------------------------------------------------
# Local microservice fleet (gateway + per-domain backends) via scripts/dev-micro.sh.
# Bring up deps first: `make ray-up`; S3/HCP from .env. The gateway listens on
# :8888 so the frontends' /api proxy works. (The `viewer` monolith target and the
# local postgres/alembic targets died at P7a — the app DB is gone; the lineage/
# openfga databases are chart-provisioned.)
dev-micro:
	uv sync --all-packages
	./scripts/dev-micro.sh

# ---- frontends (SvelteKit microfrontends) ----------------------------------
# Six independent SvelteKit SSR zones (svelte-adapter-bun) + the shared ui
# library's watcher, orchestrated by Turborepo. Each zone's Vite dev server proxies
# /api/* → VIEWER_BACKEND (the gateway, :8888). The zones come up on
# their own ports AND Turborepo auto-starts its built-in microfrontends proxy (from
# frontend/microfrontends/home/microfrontends.json — no extra package) on :3024:
#   single origin → http://localhost:3024   (browse THIS for cross-zone nav)
#   home :5273 (catch-all: /) · media :5173 /explorer · lakehouse :5174 /lakehouse · compute :5175 /compute · studio :5176 /studio · annotator :5177 /annotator
# The shared ui-package shell + nav render with NO backend; start one
# (`make dev-micro`) only when you need live /api data.

# The zone estate — one entry per directory under frontend/microfrontends/. Drives
# `make k3s-build`/`k3s-import` (one image per zone via --build-arg APP=$z) and
# sync-favicons; the zone-contract deploy-path gate pins this list to the zone
# directories that actually exist, so add/retire a zone HERE too.
ZONES ?= home lakehouse explorer annotator compute studio train

dev-frontends:        # build the ui + api libs once, then all zones + :3024 proxy
	# Build the libs FIRST so the zones read a complete dist/. Running `turbo run dev`
	# unfiltered also starts the ui library's `svelte-package -w` watcher, which rewrites
	# dist/ concurrently and races the zones reading it (one zone crashes → turbo tears
	# the whole run down). Zones-only dev avoids that. To live-edit the ui library, run
	# its watcher in a second terminal: `bun run dev:ui`.
	bunx turbo --cwd=frontend run build --filter='./packages/ui' --filter='./packages/api'
	bunx turbo --cwd=frontend run dev --filter='./microfrontends/*'

dev-frontends-k3s:    # frontend HMR (Path A) against the IN-CLUSTER backend
	# Port-forwards the in-cluster gateway to :8888, then runs all 6 zones with Vite
	# HMR pointed at it (VIEWER_BACKEND client proxy + RASK_GATEWAY_URL SSR both
	# default to :8888). One Ctrl-C tears down both (trap kills the port-forward).
	# Needs the cluster up (`make k3s-up`). Browse http://localhost:3024.
	@echo "==> port-forward rask-gateway → http://localhost:8888 (Ctrl-C stops both)"
	@$(KUBECTL) port-forward svc/rask-gateway 8888:8888 >/dev/null 2>&1 & \
	  PF=$$!; trap 'kill $$PF 2>/dev/null' EXIT INT TERM; \
	  until curl -sf http://localhost:8888/api/ray/health >/dev/null 2>&1; do sleep 1; done; \
	  echo "==> gateway reachable; starting frontends (Vite HMR)"; \
	  VIEWER_BACKEND=http://localhost:8888 RASK_GATEWAY_URL=http://localhost:8888 \
	    bunx turbo --cwd=frontend run build --filter='./packages/ui' --filter='./packages/api' && \
	  VIEWER_BACKEND=http://localhost:8888 RASK_GATEWAY_URL=http://localhost:8888 \
	    bunx turbo --cwd=frontend run dev --filter='./microfrontends/*'

home:      # catch-all zone only, :5273 (serves /)
	bun --cwd=frontend run dev:home

frontend-%:           # run one domain zone on its own port, e.g. `make frontend-explorer`
	bun --cwd=frontend run dev:$*

frontend-build:       # production-build every zone + the ui library (turbo, cached)
	bun --cwd=frontend run build

frontend-check:       # svelte-check every zone + the ui library (turbo)
	bun --cwd=frontend run check

sync-favicons:        # copy the shared favicon source → every zone's static/ (one source of truth)
	@for a in $(ZONES); do \
	  mkdir -p frontend/microfrontends/$$a/static && \
	  cp frontend/assets/favicon.ico frontend/assets/favicon.svg \
	     frontend/microfrontends/$$a/static/ ; \
	done; echo "synced favicon.{ico,svg} → 6 zones' static/"

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
	uv run --project runners/htr python runners/htr/scripts/deploy_serve.py up

serve-down:
	uv run --project runners/htr python runners/htr/scripts/deploy_serve.py down

serve-status:
	uv run --project runners/htr python runners/htr/scripts/deploy_serve.py status

# Single CPU/1-GPU htrflow endpoint for the low-resource / local-k3s shape.
serve-up-htrflow:
	RASK_SERVE_REPLICAS=1 RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --project runners/htr --no-sync python runners/htr/scripts/deploy_serve.py up --app htrflow

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
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --project runners/htr --no-sync python runners/htr/scripts/deploy_serve.py up --app transcribe
	RASK_SERVE_REPLICAS=$(RASK_SERVE_REPLICAS) RASK_SERVE_GPU_FRAC=$(RASK_SERVE_GPU_FRAC) \
	  RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --project runners/htr --no-sync python runners/htr/scripts/deploy_serve.py up --app htrflow

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

# ---- EAD harvest -----------------------------------------------------------
# (search-index / search-index-fresh died at P7a with scripts/index_alto.py;
# catalog-index died in the R6/R20 wave with scripts/index_catalog.py — the EAD
# data re-lands as a catalog-governed Lance table served at /api/explorer/search.
# harvest-ead survives: it only downloads the EAD source files.)
harvest-ead:
	uv run python scripts/harvest_ead.py

# ---- dev seeding -----------------------------------------------------------
# A fresh install serves an EMPTY corpus (explorer.corpus.mode defaults to emptyDir), so /explorer finds
# nothing, /annotator has no page and /lakehouse lists no tables — and "not seeded" is
# indistinguishable from "not built". One command fixes that for the whole estate.
seed-dev: ## Seed the dev estate: media corpus + a labeling task with items (needs a running cluster)
	scripts/seed_dev_estate.sh

# ---- rustfs (S3-compatible object storage) smoke ---------------------------
# Prove packages/storage + LanceDB work against a REAL rustfs backend (not moto).
# rustfs serves the S3 API on :9000; rask is storage-agnostic, so this is env-only.
rustfs-up: ## Start a local rustfs S3 server in docker (:9000, rustfsadmin/rustfsadmin)
	docker run -d --name rask-rustfs -p 9000:9000 \
	  -e RUSTFS_ACCESS_KEY=rustfsadmin -e RUSTFS_SECRET_KEY=rustfsadmin \
	  rustfs/rustfs:latest
	@echo "rustfs up on http://localhost:9000 (access/secret: rustfsadmin). Smoke: make smoke-rustfs"

rustfs-down:
	docker rm -f rask-rustfs

smoke-rustfs: ## Storage smoke vs rustfs (S3 round-trip + LanceDB) — needs rustfs-up
	RASK_S3_ENDPOINT_URL=http://localhost:9000 \
	  AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin \
	  RASK_S3_INSECURE=1 RASK_SMOKE_BUCKET=rask-rustfs-smoke \
	  uv run python scripts/smoke_rustfs.py

# ---- local k3s ------------------------------------------------------------
COMPOSE_IMAGES = gateway compute controlplane
# SvelteKit SSR microfrontend zone images — one web-<zone> image per $(ZONES) entry,
# all built from the one parametrized .docker/frontend.dockerfile via --build-arg
# APP=<name>. (R22: the web- prefix keeps the zone image namespace disjoint from the
# fleet's — the compute SERVICE image owns the bare `compute` name, and there is a
# compute ZONE.)
# "home" is the catch-all; the rest are pinned to their /<zone> base path.
KUBECONFIG ?= /etc/rancher/k3s/k3s.yaml
HELM ?= KUBECONFIG=$(KUBECONFIG) helm
KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl
# lance-rest-catalog is the ONE lakehouse image (catalog + lineage + medallion + maintenance +
# media trio — chart `image.catalog`); the default render runs 8 containers from it, so the
# build/import set must carry it or kind/k3s deploys ImagePullBackOff on every lakehouse pod.
K3S_IMAGES = $(COMPOSE_IMAGES) $(ZONES:%=web-%) ray-cluster lance-rest-catalog

# Subchart repos (Chart.yaml dependencies). OCI deps (kueue) need no repo add.
K3S_DEP_REPOS = nvdp=https://nvidia.github.io/k8s-device-plugin \
                kuberay=https://ray-project.github.io/kuberay-helm/ \
                nats=https://nats-io.github.io/k8s/helm/charts/ \
                dapr=https://dapr.github.io/helm-charts/ \
                openfga=https://openfga.github.io/helm-charts \
                cnpg=https://cloudnative-pg.github.io/charts \
                greptime=https://greptimeteam.github.io/helm-charts/ \
                perses=https://perses.github.io/helm-charts

k3s-install: ## One-time host bootstrap: k3s + helm only (everything else is the chart; sudo)
	./scripts/k3s-install.sh

k3s-deps: ## Add subchart repos + vendor chart dependencies into chart/charts/
	@for r in $(K3S_DEP_REPOS); do n=$${r%%=*}; u=$${r#*=}; helm repo add $$n $$u >/dev/null 2>&1 || true; done
	@helm repo update >/dev/null
	helm dependency build ./chart

k3s-build: ## Build all fleet + frontend zone + ray-cluster images as :dev (via Dagger)
	@for s in $(COMPOSE_IMAGES); do \
	  bash scripts/dagger-image.sh --name $$s --tag $$s:dev || exit 1; \
	done
	@for a in $(ZONES); do \
	  bash scripts/dagger-image.sh --zone $$a --tag web-$$a:dev || exit 1; \
	done
	bash scripts/dagger-image.sh --name ray-cluster --tag ray-cluster:dev
	# The lakehouse fleet image — dockerfile name (rest-catalog) != image name, so it can't
	# ride the COMPOSE_IMAGES loop. Same build scripts/e2e_stack.sh does.
	bash scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev

k3s-import: ## Side-load :dev images into k3s containerd
	@for s in $(K3S_IMAGES); do \
	  echo ">> importing $$s:dev"; \
	  docker save $$s:dev | sudo k3s ctr images import - || exit 1; \
	done

k3s-up: k3s-deps ## Vendor deps, then install/upgrade the rask release and wait for the gateway
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ -z "$$HF_TOKEN" ] && [ -r "$${HF_HOME:-$$HOME/.cache/huggingface}/token" ]; then \
	  HF_TOKEN="$$(cat "$${HF_HOME:-$$HOME/.cache/huggingface}/token")"; \
	  echo ">> HF_TOKEN from $${HF_HOME:-$$HOME/.cache/huggingface}/token (hf auth login)"; \
	fi; \
	if [ -z "$$HF_TOKEN" ]; then echo "WARN: no HF token (env, .env, or 'hf auth login') — htrflow Serve will 401 on the gated TrOCR model"; fi; \
	$(HELM) upgrade --install rask ./chart --wait --wait-for-jobs --timeout 20m \
	  --take-ownership \
	  --set image.localImages=true \
	  $${HF_TOKEN:+--set-string secrets.hfToken=$$HF_TOKEN} \
	  $${AWS_ACCESS_KEY_ID:+--set-string rustfs.accessKey=$$AWS_ACCESS_KEY_ID} \
	  $${AWS_SECRET_ACCESS_KEY:+--set-string rustfs.secretKey=$$AWS_SECRET_ACCESS_KEY}
	$(KUBECTL) rollout status deploy/rask-gateway --timeout=300s
	@echo "UI → http://<node-ip>/   (catch-all ingress; over VS Code/ssh -L forward port 80 → http://localhost:<port>/)"
	@echo "API → http://<node-ip>/api/ray/health"

k9s: bootstrap ## Browse the k3s cluster in k9s (the chart's NOTES.txt points here)
	@KUBECONFIG=$(KUBECONFIG) $(LOCALBIN)/k9s

k3s-down: ## Uninstall the rask release (keep PVCs)
	$(HELM) uninstall rask || true

k3s-purge: k3s-down ## Uninstall + delete PVCs (clean slate)
	$(KUBECTL) delete pvc -A -l app.kubernetes.io/instance=rask --ignore-not-found || true
	# Stale PVCs left in old per-component namespaces (dapr/nats now run in the
	# release namespace) that the label-scoped delete above misses — reclaim the
	# storage without removing the namespaces themselves. Idempotent.
	-$(KUBECTL) delete pvc -n dapr-system --all --ignore-not-found
	-$(KUBECTL) delete pvc -n nats --all --ignore-not-found

# ---- kind (throwaway CI-shaped cluster; k3s above is the long-lived local deploy) ----
# Same chart, same :dev image set, same release name (rask) as k3s — but on a disposable
# kind cluster, which is what the CI live-proof jobs (e2e-stack / e2e-ray) boot. Toolchain
# is pinned into .localbin by `make bootstrap` (kind/kubectl/fga; helm + docker from PATH).
.PHONY: bootstrap kind-up kind-images kind-load kind-deploy kind-down e2e-ci e2e-ray-ci

LOCALBIN     := $(CURDIR)/.localbin
KIND         := $(LOCALBIN)/kind
KIND_CLUSTER := rask
KIND_IMAGES  := $(K3S_IMAGES)
# OS/arch detection so bootstrap works on Linux/macOS, x86_64/arm64. sed/tr, NOT a shell
# `case`: a `)` inside $(shell …) prematurely closes make's paren-match.
HOST_OS   := $(shell uname -s | tr '[:upper:]' '[:lower:]')
HOST_ARCH := $(shell uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
KIND_V    := v0.25.0
KUBECTL_V := v1.31.3
FGA_V     := 0.6.4
K9S_V     := v0.32.7
# k9s tags its assets Linux/Darwin (capitalised), unlike everything else above.
K9S_OS    := $(shell uname -s)

bootstrap: ## Download kind/kubectl/fga into .localbin (idempotent) — helm + docker must be on PATH
	@mkdir -p $(LOCALBIN)
	@test -n "$(HOST_ARCH)" || { echo "!! unsupported CPU '$$(uname -m)' — need x86_64 or arm64"; exit 1; }
	@test -x $(KIND)              || { echo "kind ->"; curl -fsSL -o $(KIND) "https://kind.sigs.k8s.io/dl/$(KIND_V)/kind-$(HOST_OS)-$(HOST_ARCH)" && chmod +x $(KIND); }
	@test -x $(LOCALBIN)/kubectl  || { echo "kubectl ->"; curl -fsSL -o $(LOCALBIN)/kubectl "https://dl.k8s.io/release/$(KUBECTL_V)/bin/$(HOST_OS)/$(HOST_ARCH)/kubectl" && chmod +x $(LOCALBIN)/kubectl; }
	@test -x $(LOCALBIN)/fga      || { echo "fga ->"; curl -fsSL "https://github.com/openfga/cli/releases/download/v$(FGA_V)/fga_$(FGA_V)_$(HOST_OS)_$(HOST_ARCH).tar.gz" | tar xz -C $(LOCALBIN) fga; }
	@test -x $(LOCALBIN)/k9s      || { echo "k9s ->"; curl -fsSL "https://github.com/derailed/k9s/releases/download/$(K9S_V)/k9s_$(K9S_OS)_$(HOST_ARCH).tar.gz" | tar xz -C $(LOCALBIN) k9s; }
	@command -v docker >/dev/null || { echo "!! docker not on PATH — install https://docs.docker.com/get-docker/"; exit 1; }
	@command -v helm   >/dev/null || { echo "!! helm not on PATH — install https://helm.sh/docs/intro/install/"; exit 1; }
	@echo "toolchain ready in .localbin ($(HOST_OS)/$(HOST_ARCH))"

kind-up: bootstrap ## Create the rask kind cluster (idempotent; deploy/kind/kind-config.yaml)
	@$(KIND) get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
	  $(KIND) create cluster --name $(KIND_CLUSTER) --config deploy/kind/kind-config.yaml --wait 150s

kind-images: k3s-build ## Build the full :dev image set (same builds k3s uses — fleet + zones + ray-cluster)

kind-load: ## Side-load the :dev image set into the kind cluster
	$(KIND) load docker-image $(foreach i,$(KIND_IMAGES),$(i):dev) --name $(KIND_CLUSTER)

# Third-party images the chart pulls (dapr x5, nats, openfga, cnpg, greptimedb, perses, kuberay,
# kueue, dex, openbao, otel-collector, …). kind's containerd is a SEPARATE image store from the
# host's docker, so a fresh cluster re-pulls every one of them from the internet — ~20 minutes of
# pure network before a single app pod starts. The host docker cache, by contrast, persists across
# clusters: pull once here, side-load, and every later cluster starts in seconds. Derived from the
# actual render (not a hand-list, which would rot) with our own :dev images filtered out. An image
# with NO tag is skipped (a subchart default the render leaves incomplete — the pod that resolves it
# pulls it). The character classes are DELIBERATELY case-sensitive-inclusive: docker tags allow
# uppercase, and a lowercase-only class silently TRUNCATED `apache/age:release_PG16_1.5.0` to
# `apache/age:release_` and `minio/mc:RELEASE.…` to `minio/mc:` — so the two images the OpenFGA
# migration and the bucket-init hook block on were the only ones this target never preloaded, which
# is exactly backwards.
kind-preload: bootstrap ## Pull the chart's third-party images into the host cache, then side-load them
	@imgs=$$(helm template rask ./chart --set singleTenant.enabled=true --set explorer.enabled=true \
	    --set observability.enabled=true 2>/dev/null \
	  | grep -oE 'image: *"?[A-Za-z0-9./:@_-]+' | sed 's/image: *"\?//' \
	  | grep -vE "^($$(echo '$(K3S_IMAGES)' | tr ' ' '|'))(:|$$)" \
	  | grep -E ':[A-Za-z0-9][A-Za-z0-9._-]*$$' | sort -u); \
	  echo "$$imgs" | sed 's/^/  + /'; \
	  for i in $$imgs; do docker image inspect $$i >/dev/null 2>&1 || docker pull -q $$i || \
	    echo "  !! pull failed (skipped): $$i"; done; \
	  for i in $$imgs; do docker image inspect $$i >/dev/null 2>&1 && \
	    $(KIND) load docker-image $$i --name $(KIND_CLUSTER) >/dev/null 2>&1 || true; done; \
	  echo "==> third-party images cached on the host and side-loaded into kind-$(KIND_CLUSTER)"

# `--wait` is SAFE here since the bootstrap Jobs left the hook lifecycle (see "BOOTSTRAP JOBS" in
# chart/templates/_helpers.tpl). It used to deadlock: the OpenFGA migration was a post-install hook,
# and post-install hooks run AFTER --wait, which was itself blocked on the server the migration had to
# unblock. Keeping --wait off was the workaround; keeping it ON is now the regression test.
kind-deploy: k3s-deps ## helm upgrade --install release `rask` into the kind cluster
	helm upgrade --install rask ./chart --kube-context kind-$(KIND_CLUSTER) --wait --wait-for-jobs --timeout 900s
	$(LOCALBIN)/kubectl --context kind-$(KIND_CLUSTER) rollout status deploy/rask-gateway --timeout=300s

kind-down: ## Delete the rask kind cluster
	$(KIND) delete cluster --name $(KIND_CLUSTER)

# THE guarded live proofs — identical to the CI `e2e-stack` / `e2e-ray` jobs (both shell out
# to the same scripts, so "green in CI" and "green on my machine" cannot diverge). The
# scripts bring up their own governed kind stack, seed grants/buckets, run the suites, and
# (in CI) tear the cluster down.
e2e-ci: bootstrap ## Governed kind stack + the 5 live e2e suites (CAS/#2/#3-A/#3-B/#4) == CI e2e-stack
	CLUSTER=$(KIND_CLUSTER) RELEASE=rask bash scripts/e2e_stack.sh

e2e-ray-ci: bootstrap ## Governed ray-ON kind stack + real KubeRay + both Ray suites == CI e2e-ray
	CLUSTER=$(KIND_CLUSTER)-ray-e2e RELEASE=rask bash scripts/ray_e2e_stack.sh

# ---- the local image path (registry + Dagger engine) ------------------------
# `make dev-registry` once per host (a local registry on :5000, and k3s pointed at it), and
# `make dagger-engine` once (an engine that may push to a plain-HTTP registry). Every
# `dagger call image|zone-image ... publish` goes through both. Iterate the frontends with
# `make dev-frontends` (local Vite HMR); backend changes rebuild and `make k3s-up`.
dagger-engine: ## One-time: a Dagger engine that can push to the plain-HTTP dev registry
	@bash scripts/dagger-engine.sh

# ---- reclaiming the dev loop's disk -----------------------------------------
# Both of these grow WITHOUT BOUND if nobody looks, and they grew to ~1.2 TB before anyone did:
#   * the Dagger engine cache — its default GC ceiling is proportional to the disk (5.67 TB on a 7.4 TB
#     volume), so it never collected. `make dagger-engine` now writes an explicit 60 GB cap; this target
#     is the manual sweep.
#     old ones (measured: 113 tags of web-home, 83.9 GB).
# Both are safe: every artefact is reproducible with `dagger call image|zone-image ... publish`, and k3s
# holds running images in its own containerd cache.
registry-gc: ## Drop all but the newest tags in the dev registry, then sweep blobs
	@bash scripts/registry-gc.sh

dagger-gc: ## Prune the Dagger engine's build cache to its configured ceiling
	@_EXPERIMENTAL_DAGGER_RUNNER_HOST=docker-container://$${DAGGER_ENGINE_NAME:-dagger-engine-rask} \
	  dagger core engine local-cache prune

dev-gc: registry-gc dagger-gc ## Reclaim both the registry and the Dagger cache
	@df -h / | tail -1 | awk '{print ">> disk: "$$4" free, "$$5" used"}'

dev-registry: ## One-time: local image registry + point k3s at it (sudo; restarts k3s)
	bash scripts/k3s-registry.sh

# All THREE reload paths, because they fail independently and each one was broken on its own at some
# point: a wheel synced into site-packages, a zone's COMPILED build/, and the ui package's dist/ (the only
# package with a build step — zones bundle its dist, never its source).
# ---- e2e (Playwright) -------------------------------------------------------
e2e: ## Browser e2e against a running deploy (RASK_E2E_BASE_URL, default http://localhost)
	cd tests/e2e && bun install && bunx playwright test
