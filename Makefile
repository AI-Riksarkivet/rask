.PHONY: fga-store-check fga-estate-migrate sbom registry-gc dagger-gc dev-gc help install build test test-slow lint lint-imports fmt clean storybook typecheck knip comment-gate check coverage fga-test ci dev dev-k3s frontend-build frontend-check console-image claude-bootstrap k3s-install k3s-fixtures k3s-deps k3s-build k3s-import k3s-up k3s-values k3s-values-check k3s-down k3s-purge k9s bootstrap dev-registry prod-render-check chart-docs chart-docs-check alert-rules-check alert-rules-drill audit smoke-rustfs rustfs-lifecycle rustfs-provisioning governance-chain nats-auth go-fmt scan-config scan-secrets scan-image seed-dev e2e-isolation e2e-container-deletes e2e-fga-model

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck knip check ci   frontend-check frontend-build"
	@echo "  dev                                    — the console (Vite HMR, /api → RASK_EDGE_URL)"
	@echo "  dev-k3s                                — the console against the IN-CLUSTER edge (Traefik, port-forwarded)"
	@echo "  console-image                          — the console's static-file image (web + serve.ts)"
	@echo "  dev-gc                                 — reclaim the dev registry + the Dagger cache"
	@echo "  claude-bootstrap                       — install Claude Code skills & verify config"

# --all-packages, not a bare `uv sync`: a bare sync installs the ROOT project only and
# omits every workspace member, so a fresh clone gets a venv where `import service_kit`
# fails. Same flag as .dagger — one behaviour everywhere.
install:
	bun --cwd=frontend install
	uv sync --all-packages
	@# BOTH hook types, and the second one is the reason this line exists. `prek install` with no
	@# --hook-type writes only `.git/hooks/pre-commit`, so every hook declared `stages = ["commit-msg"]`
	@# is configured and never runs. Measured 2026-09-10: `conventional-pre-commit` and
	@# `no-co-authored-by-claude` had both been dead the whole time, and 108 commits in 24 hours carried
	@# the trailer the second one exists to refuse. A gate that cannot fire is worse than no gate — it
	@# reads as enforcement in the config while the rule goes unheld.
	@command -v prek >/dev/null 2>&1 && prek install --hook-type pre-commit --hook-type commit-msg || echo "  (prek not on PATH — git hooks NOT installed)"
	@# The Claude Code plugins (ra-skills, the mattpocock skills) and the svelte MCP server install here too,
	@# so a new machine does not depend on someone remembering a second command.
	@# The chart tests render chart/ and the fixture charts, so `make test` needs their subcharts on disk.
	@command -v helm >/dev/null 2>&1 && $(MAKE) --no-print-directory k3s-deps || echo "  (helm not on PATH — subcharts NOT fetched; the chart tests in make test will fail until make k3s-deps runs)"
	@command -v claude >/dev/null 2>&1 && $(MAKE) --no-print-directory claude-bootstrap || echo "  (claude CLI not on PATH — Claude Code plugins NOT installed; run make claude-bootstrap after installing it)"

build:
	uv sync --all-packages
	bun --cwd=frontend run build

# Python tests via pytest. The console has its own unit suite (vitest): `bun --cwd=frontend run test` runs it, and CI
# runs both, so this target covers ONE plane.
# `not e2e`: tests/e2e-py is collectable (so the collection gate in
# tests/unit/test_e2e_collection_gate.py can see it) but its suites need a LIVE deployed
# stack — run them via `make e2e-ci` / the per-suite targets.
# PARALLEL BY DEFAULT, and it is the difference between a loop someone runs and a loop someone skips.
# Measured 2026-09-24 on this host (nproc 64): the default testpaths take **854 s** in one process and
# **102 s** at `-n 16` — 8.4x, with identical results (10,357 passed / 161 skipped / 2 xfailed both
# ways). `pytest-xdist` was already a dependency and nothing used it.
#
# NOT IN `addopts`, deliberately. That would also bound the model-loading `slow` suites and every
# in-container run, where `nproc` reads the HOST's core count while `cpu.max` grants one — the same
# mismatch `chart/values.yaml` records for the maintenance pod. A target-level default an operator can
# override is the honest shape: `make test PYTEST_WORKERS=4`, or `PYTEST_WORKERS=0` for one process
# when a failure needs a readable traceback.
PYTEST_WORKERS ?= 16
PYTEST_PARALLEL = $(if $(filter 0,$(PYTEST_WORKERS)),,-n $(PYTEST_WORKERS))

# Every dagger call in this file names the engine it runs on. An unscoped CLI that provisions an engine of its own
# removes the other `dagger-engine-*` containers, the estate's `dagger-engine-rask` among them (measured on dagger
# 0.21.8 and 1.0), so a call goes to the engine `make dagger-engine` creates or fails. CI calls dagger directly, on the
# engine its runner provisions, never through these targets.
DAGGER_ENGINE_NAME ?= dagger-engine-rask
DAGGER = _EXPERIMENTAL_DAGGER_RUNNER_HOST=docker-container://$(DAGGER_ENGINE_NAME) dagger

# The OpenFGA server the offline suite asserts authorization outcomes against (root `conftest.py::openfga`, [[LH-235]]):
# the static binary out of the very image `.dagger/governed.go` pins and `dagger call test` copies onto PATH, so a
# developer box and CI evaluate the model with one server. It depends on that file, so a bumped pin re-exports it.
OPENFGA_BIN := $(CURDIR)/.localbin/openfga
OPENFGA_IMAGE = $(shell sed -n 's/^[[:space:]]*openfgaImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/governed.go)
$(OPENFGA_BIN): .dagger/governed.go
	@test -n "$(OPENFGA_IMAGE)" || { echo "!! no openfgaImage pin read from .dagger/governed.go"; exit 1; }
	@mkdir -p $(dir $@)
	$(DAGGER) core container from --address=$(OPENFGA_IMAGE) file --path=/openfga export --path=$@
	@chmod +x $@ && touch $@
.PHONY: openfga
openfga: $(OPENFGA_BIN) ## Export the pinned OpenFGA server binary to .localbin, for the suite's authorization tests

test: $(OPENFGA_BIN)
	uv run pytest -m "not slow and not e2e" $(PYTEST_PARALLEL)

# `slow` marks a long-running offline test; this target runs them with the rest of the offline suite.
test-slow: $(OPENFGA_BIN)
	uv run pytest -m "not e2e" $(PYTEST_PARALLEL)

lint:
	uv run ruff check .
	uv run python scripts/lakehouse_names_no_client.py
	bun --cwd=frontend run lint

# The import-layer contracts in `.importlinter` (the lakehouse's, which name no client module), then each member's
# own: every workspace member whose pyproject declares `[tool.importlinter]`, none today. A member's contract travels
# with its tree, so this target names no member and holds with any member's tree deleted (XC-188). `uv run --with` layers the pinned
# import-linter over the workspace venv, so the graph it walks is the workspace's own editable installs.
lint-imports:
	uv run --with import-linter==2.15 lint-imports
	@for config in $$(grep -l '^\[tool\.importlinter\]' services/*/pyproject.toml packages/*/pyproject.toml); do \
		echo "lint-imports --config $$config"; \
		uv run --with import-linter==2.15 lint-imports --config "$$config" || exit 1; \
	done

fmt:
	uv run ruff format .
	bun --cwd=frontend run fmt

storybook:
	bun --cwd=frontend run storybook

# ---- comment rule (forward-only; docs/adr/0044-comments-carry-rationale-and-provenance-never-a-changelog.md) -------------------------
# Kind (3) prose — a comment whose subject is a previous comment — is banned in NEW code. The 35,279
# lines already written are deliberately left alone, so this reads ADDED lines only and is green at
# HEAD by construction. `prek` runs the same script on `--staged`; CI should run it as
# `--base origin/main`. `--report` prints the standing population and always exits 0.
# NOT wired into `make check`: check is a whole-tree gate and this one is a diff gate, and pretending
# otherwise is how a diff gate ends up either red for everyone or scoped to nothing.
comment-gate:
	uv run python scripts/comment_history_gate.py --worktree

clean:
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/storybook-static

# ---- python (uv workspace) -------------------------------------------------
typecheck:
	uv run --frozen ty check

# ---- OpenAPI contract (docs/*-openapi.json) --------------------------------
# The committed specs ARE the reviewable contract, generated from the live FastAPI
# apps. CI enforces drift via `dagger call openapi` (.dagger/openapi.go, ci.yml) —
# these targets are the same gate, reachable locally. Both scripts/gen_openapi.py
# and .dagger/openapi.go referenced them by name for months before they existed.
# `--all-packages` is load-bearing: the root pyproject has `dependencies = []`, so a plain
# `uv run` installs no workspace member and `import catalog.main` fails. CI gets this via
# .dagger/test.go's `uv sync --all-packages`; locally we do the same.
#
# THE SPEC HAS TWO READERS and the generator writes both: the JSON, and the TypeScript client
# openapi-typescript derives from it. Regenerating only the JSON leaves the client naming a value set
# the bus no longer sends — which is unobservable to every Python gate, so the target does both and
# the check diffs all four files.
openapi:
	uv sync --all-packages
	uv run --no-sync scripts/gen_openapi.py
	bun --cwd=frontend run gen:types

# Diffs against HEAD, not the index: `git diff` alone compares to the index, so a *staged*
# drifted spec would pass falsely. CI's dagger function snapshots the committed files and
# diffs the regenerated output back, which is what `git diff HEAD` reproduces.
openapi-check: openapi
	@git diff HEAD --exit-code -- docs/catalog-openapi.json docs/lineage-openapi.json \
	  frontend/packages/api/src/generated/catalog.ts frontend/packages/api/src/generated/lineage.ts \
	  || { echo "!! OpenAPI drift: a route changed without refreshing the spec or its client. Commit the diff above."; exit 1; }
	@echo "OpenAPI specs match the committed contract"

# ---- the console image -----------------------------------------------------
# The console is one static single-page app served by its own `serve.ts` (.docker/frontend.dockerfile). The build goes
# through DAGGER (scripts/dagger-image.sh -> `dagger call image --name=frontend`), like every image in this repository;
# the helper supplies the OCI provenance args (BUILD_DATE / VCS_REF / VERSION), because a Dagger Function is sandboxed
# and cannot read git.
CONSOLE_TAG ?= dev

console-image: ## Build the console's image (web-console:$(CONSOLE_TAG)) from .docker/frontend.dockerfile
	VERSION="$(CONSOLE_TAG)" bash scripts/dagger-image.sh --name frontend --tag web-console:$(CONSOLE_TAG)

# ---- chart gates (prod overlay + alert rules) -------------------------------
# CI enforces both via `dagger call charts` (.dagger/charts.go), which shells out to these two
# targets BY NAME — and neither existed, so `dagger call charts` died at the first of them and the
# prod-overlay HA/security invariants and the alert-rule proofs had never once run. Exactly the
# defect the OpenAPI note above records ("referenced them by name for months before they existed"),
# repeated one section down; and both assets already specify their own commands — scripts/
# prod_render_check.sh's header says "Run: `make prod-render-check`", and deploy/alerting/
# rules_test.yml's says this target runs check-then-test. Only the wrappers were missing.
#
# Bare `helm`/`promtool` on purpose: the Dagger container curls both to /usr/local/bin and
# deliberately excludes .localbin so a host-downloaded binary cannot shadow the pinned one.
prod-render-check: ## Render values-prod.yaml and assert its HA + security switches are ON
	./scripts/prod_render_check.sh

# `promtool` is resolved, not assumed. A bare `promtool` had never been on PATH here: `make bootstrap`
# installs it into .localbin and NOTHING prepends that to PATH — `.dagger/charts.go` asserted in a
# comment that "the Makefile's `export PATH := $(LOCALBIN):…` prepend" existed, and `git log -S` finds
# no such line has EVER been in this file. So this target died with "make: promtool: No such file or
# directory" on any box without a globally-installed promtool, and it died in CI too for a different
# reason (it is step 7 of charts.go's chain, and step 2 had been failing since 2026-08-04). Nothing has
# run these alert rules in either place.
# PATH first, .localbin second, deliberately: inside the Dagger gate container the pinned
# /usr/local/bin/promtool is on PATH and .localbin is excluded from the mount, so PATH wins there and
# the pinned binary stays the single source of truth — which is what that charts.go comment MEANT.
alert-rules-check: ## promtool: the alert rules are valid AND actually fire on synthetic series
	@PROMTOOL=$$(command -v promtool 2>/dev/null || echo $(LOCALBIN)/promtool); \
	  [ -x "$$PROMTOOL" ] || { echo "!! promtool not found on PATH or in $(LOCALBIN) — run 'make bootstrap'"; exit 1; }; \
	  "$$PROMTOOL" check rules deploy/alerting/rules.yml && \
	  "$$PROMTOOL" test rules deploy/alerting/rules_test.yml

# promtool is PROMETHEUS. Production is vmalert -> GreptimeDB, and the two do not accept the same
# PromQL: two rules shipped that promtool called SUCCESS and GreptimeDB answered 400/500, so they
# could never fire and nothing reported it. This replays every rule against the real engine.
# Exit 2 (no datasource) is a SKIP so a laptop without a cluster is not a red build — which means it
# is the k3s/CI run that actually proves anything. Run it before shipping a rule change.
alert-rules-drill: ## replay every alert rule against a REAL GreptimeDB (skips with no cluster)
	@uv run python scripts/alert_rules_drill.py; s=$$?; [ $$s -eq 1 ] && exit 1 || exit 0

# ---- frontend dead-code + dep gate (knip, workspace-wide; see knip.json) ---------
# Analyses the whole JS graph at once, so it runs once over the workspace rather than per package.
knip:
	bun --cwd=frontend run knip

# Coverage ON REQUEST, not on every run. `--cov` used to sit in pytest's `addopts`, so every
# invocation in the estate — local, CI, Dagger — computed a report that nothing read: no `fail_under`,
# no xml, no artifact upload, no threshold anywhere. Pure cost on the merge path.
#
# The denominator is every src file of every workspace member, whichever tests ran: `[tool.coverage.run]
# source` names the two workspace directories and `[tool.coverage.report] include_namespace_packages`
# makes discovery walk their src-layout members (without it, a single package's test run discovered 3
# files against 427), and `omit` keeps every `__init__.py`, where several members keep real
# implementation (an app factory, a route table).
#
# NO `fail_under` HERE ON PURPOSE. Picking the number is an owner's decision, and a threshold invented
# by whoever fixed the plumbing is how a gate ends up ratcheted to whatever happened to be true that
# afternoon. The report is honest now; the policy is a separate call.
coverage: $(OPENFGA_BIN) ## The coverage report, computed on request against a correct denominator
	uv run pytest -m "not e2e and not slow" --cov --cov-report=term-missing:skip-covered

# `go-fmt` IS IN HERE because a gate that runs nowhere is not a gate. It gated the Go plane from no
# caller at all: not this target, not the CI gate matrix, only a `make go-fmt` nobody types — and
# `.dagger/storage.go` was sitting unformatted in a pushed commit when that was measured (2026-09-24).
check: fmt lint lint-imports typecheck knip fga-test go-fmt chart-docs-check

chart-docs: ## Regenerate chart/README.md and chart/values.schema.json from values.yaml (helm-docs, helm-schema)
	@bash scripts/chart_docs.sh

chart-docs-check: ## Fail when chart/README.md or values.schema.json is stale, or a chart value has no `# --` description
	@bash scripts/chart_docs.sh --check

# The authorization model's OWN suite, plus the drift check between its three copies. Both halves lived
# ONLY in `.github/workflows/ci.yml`, so `make ci` could be fully green on a machine where the model's
# evaluation semantics had never been evaluated — and "runs only in CI" is a synonym for "runs when
# that job happens to be one of the green ones". Wired into `check` so the default developer gate
# covers it; `fga` is already pinned into .localbin by `make bootstrap`.
#
# THE MODEL EXISTS IN THREE COPIES and only one of them is what the app loads: `model.fga` is authored,
# `model.fga.yaml` is what `fga model test` evaluates, and `model.json` is what the service reads at
# runtime. Testing the yaml while shipping the json is how a tested model and a deployed model drift
# apart in silence, so the transform-and-diff is not optional decoration — it is the half that makes
# the other half mean anything.
# `validate` reads `.is_valid` rather than the exit code: measured with fga v0.6.4, an INVALID model
# prints `{"is_valid":false,...}` and exits 0, so gating on `$$?` passes on one. It runs FIRST because
# `model test` does fail on an invalid model (exit 1) but reports it as an rpc error from the test
# runner; and because `model transform` does NOT — it emits well-formed JSON from an invalid model, so
# regenerating `model.json` as `model.fga.yaml`'s header instructs makes the drift check agree.
fga-store-check: ## Does the RUNNING store hold the model this repo declares? (needs a cluster)
	@bash scripts/fga-store-check.sh

# THE TUPLES DO NOT MOVE THEMSELVES when `fga_root_object` is repointed onto the estate ([[LH-055]]).
# Run this BEFORE deploying the repoint, never after: a principal holding `owner` on the default
# warehouse passes every estate door today and none of them the moment the setting flips.
# Piped into the CATALOG'S OWN POD for the reason `fga-store-check` is — the address the code reads
# (`RASK_FGA_API_URL`) is the one that matters, and a port-forward answers for a different one.
fga-estate-migrate: ## Carry the estate privileges from the default warehouse onto the estate root (needs a cluster)
	@POD="$$(kubectl -n $${NS:-default} get pods -o name | grep -m1 'rask-catalog' | sed 's|pod/||')"; \
	[ -n "$$POD" ] || { echo "!! no rask-catalog pod in namespace $${NS:-default}"; exit 1; }; \
	echo ">> through $$POD (DRY_RUN=$${DRY_RUN:-})"; \
	kubectl -n $${NS:-default} exec -i "$$POD" -c catalog -- env DRY_RUN="$${DRY_RUN:-}" python - < scripts/fga_estate_root_migrate.py

# THE FOURTH COPY. `fga-test` below diffs `model.fga` against `model.json` — two files in the repo
# that agree with each other — and the estate already killed a three-copy lie once. The copy it still
# cannot see is the one in the STORE, which is where every live check is answered from.
# Measured 2026-09-22: with both repo files declaring `type estate`, `make fga-test` green and
# `ms-authz` green in CI, the store held ten types and no `estate`. Nothing in the repo could tell
# that state from the one after the hook ran. `fga-store-check` is the only thing that can, and it is
# deliberately NOT in `check`: it needs a live cluster, and `check` is offline.
fga-test: ## The OpenFGA model's validity + its own suite + the three-copy drift check
	@AUTH=packages/service-kit/src/service_kit/governed/auth; \
	FGA=$$(command -v fga 2>/dev/null || echo $(LOCALBIN)/fga); \
	test -x "$$FGA" || { echo "fga not found — run 'make bootstrap'"; exit 1; }; \
	"$$FGA" model validate --file $$AUTH/model.fga > $(LOCALBIN)/model.validate.json; \
	jq -e '.is_valid == true' $(LOCALBIN)/model.validate.json > /dev/null \
	  || { echo "model.fga is INVALID:"; cat $(LOCALBIN)/model.validate.json; exit 1; }; \
	"$$FGA" model test --tests $$AUTH/model.fga.yaml; \
	"$$FGA" model transform --file $$AUTH/model.fga > $(LOCALBIN)/model.transformed.json; \
	jq -S . $(LOCALBIN)/model.transformed.json > $(LOCALBIN)/model.a.json; \
	jq -S . $$AUTH/model.json > $(LOCALBIN)/model.b.json; \
	diff -u $(LOCALBIN)/model.b.json $(LOCALBIN)/model.a.json || { echo "model.json drifted from model.fga"; exit 1; }

ci: check test

# ---- supply chain (.dagger/scan.go) ----------------------------------------
# Same contract as every other gate here: `make audit` == `dagger call audit`, one definition, no
# second description of it. Deliberately NOT folded into `check` — check is offline and fast, these
# reach osv.dev and ghcr for advisory data, and a laptop on a train should not fail `make check`.
#
# audit       osv-scanner over every lockfile + .dagger/go.mod. No build, seconds.
# scan-config trivy misconfig over .docker/ + chart/, and secret detection repo-wide. No build.
# scan-image  trivy over an image DAGGER BUILT — needs the build first, so it is minutes, not seconds.
#             NAME is the .docker/<stem>.dockerfile stem (`frontend` is the console).
audit:
	$(DAGGER) call audit

# The Go plane's formatting gate. `.dagger/` implements every CI gate in this estate and nothing
# checked its own formatting — `gofmt -l` reported two tracked hand-written files unformatted. Same
# contract as `audit` above: one definition, `make go-fmt` == `dagger call go-fmt`.
#
# It runs in a container because there is NO Go toolchain on the developer PATH here, and the
# repository rule is absolute: every container goes through Dagger. `go-fmt-fixed` is the write half —
# `dagger call go-fmt-fixed export --path=.dagger` — because a gate with no paired fixer is a gate
# people work around, and here the only alternative would be installing a toolchain.
go-fmt: ## gofmt gate over the .dagger Go plane (fix: dagger call go-fmt-fixed export --path=.dagger)
	$(DAGGER) call go-fmt

scan-config:
	$(DAGGER) call scan-config

# scan-secrets reads the full GIT HISTORY, not the working tree — a credential committed and later
# deleted is still public on a public repo. Gates on VERIFIED results only (trufflehog asks the
# provider whether the key works), so it blocks in CI where the other two legs are report-only.
scan-secrets:
	$(DAGGER) call scan-secrets

scan-image:
	@test -n "$(NAME)" || { echo "  !! usage: make scan-image NAME=rest-catalog"; exit 1; }
	$(DAGGER) call scan-image --name=$(NAME)

# THE INVENTORY, not a gate. `scan-image` answers "is this image clean today"; this answers "what is
# IN it", which is the question a new CVE asks and the one nothing here could answer. Emitted from the
# same tarball the scanner reads, so the SBOM describes the exact bytes that were scanned.
sbom: ## CycloneDX SBOM for one image (NAME=rest-catalog) — writes ./sbom-<name>.cdx.json
	@test -n "$(NAME)" || { echo "  !! usage: make sbom NAME=rest-catalog"; exit 1; }
	$(DAGGER) call sbom --name=$(NAME) export --path=./sbom-$(NAME).cdx.json
	@echo "  -> ./sbom-$(NAME).cdx.json"

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

# ---- the console (one SvelteKit single-page app) --------------------------
# `frontend/console` builds to static files: no server of its own, the browser signs in to Dex with PKCE and calls the
# lakehouse's /api/* with its bearer. In development Vite serves `frontend/console/dev/config.json` as
# /config.json: the issuer `http://localhost:8080/dex` (the value DEV_ISSUER defaults to, so the token's `iss` is the one
# `make k3s-up` gives the estate) and the client `rask-console`. It proxies /api/* to RASK_EDGE_URL (default :8080): one
# origin on the host that serves both the issuer and the API, as k3s Traefik's Service does. The design system builds
# first so the app reads a complete dist/; to live-edit it, run `bun --cwd=frontend/packages/ui run dev` beside it.
dev:                  # the console with Vite HMR, /api → RASK_EDGE_URL, signing in at the issuer dev/config.json names
	bun --cwd=frontend run dev

# How long dev-k3s waits for the port-forwarded Traefik to serve both the issuer and the edge before it gives up.
DEV_K3S_WAIT ?= 60
dev-k3s:              # the console with Vite HMR against the IN-CLUSTER edge and Dex, both through k3s Traefik on :8080
	@serves() { curl -sf http://localhost:8080/dex/.well-known/openid-configuration >/dev/null 2>&1 && \
	    curl -sf http://localhost:8080/api/lineage/livez >/dev/null 2>&1; }; \
	if serves; then \
	  echo "==> :8080 already serves the issuer and the edge; using it"; \
	  RASK_EDGE_URL=http://localhost:8080 bun --cwd=frontend run dev; \
	else \
	  echo "==> port-forward kube-system/traefik → http://localhost:8080 (Ctrl-C stops both)"; \
	  $(KUBECTL) -n kube-system port-forward svc/traefik 8080:80 >/dev/null 2>&1 & \
	  PF=$$!; trap 'kill $$PF 2>/dev/null' EXIT INT TERM; \
	  deadline=$$(( $$(date +%s) + $(DEV_K3S_WAIT) )); \
	  until serves; do \
	    kill -0 $$PF 2>/dev/null || { echo "!! the port-forward exited (is :8080 held by something that is not the estate's edge?)"; exit 1; }; \
	    [ "$$(date +%s)" -lt "$$deadline" ] || { echo "!! :8080 did not serve both the issuer and /api/lineage/livez within $(DEV_K3S_WAIT) s"; exit 1; }; \
	    sleep 1; done; \
	  echo "==> issuer and edge reachable; starting the console (Vite HMR)"; \
	  RASK_EDGE_URL=http://localhost:8080 bun --cwd=frontend run dev; \
	fi

frontend-build:       # production-build the console and the design system
	bun --cwd=frontend run build

frontend-check:       # svelte-check, lint, format, unit tests and knip over the console and its packages
	bun --cwd=frontend run check && bun --cwd=frontend run lint && bun --cwd=frontend run fmt:check && \
	  bun --cwd=frontend run test && bun --cwd=frontend run knip

# ---- dev seeding -----------------------------------------------------------
# The demo estate through the catalog's real doors, in hierarchy order (scripts/seed_estate.py): projects, warehouses,
# namespaces and grants a fresh install shows nothing of.
seed-dev: ## Seed the dev estate's projects, warehouses, namespaces and grants through the catalog (needs a running cluster)
	uv run python scripts/seed_estate.py

# ---- rustfs (S3-compatible object storage) smoke ---------------------------
# Prove lakehouse_kit.storage and pylance work against a REAL rustfs backend (not moto).
# rustfs serves the S3 API on :9000; rask is storage-agnostic, so this is env-only.
# ONE command, not three. This replaced `rustfs-up` (a DETACHED `docker run`), `smoke-rustfs` and
# `rustfs-down` — the last of which an operator had to remember. The service is bound for exactly the
# life of the smoke: nothing is detached, nothing leaks when it fails, and no host port is taken, so it
# cannot collide with a `make dev-*` stack or a second developer on the same box.
#
# The old shape's `docker run` was one of the estate's last three, and the stated objection to
# converting it was that `as-service up` holds a terminal where `up -d` does not. That is true of a
# two-step shape and irrelevant to a one-step one.
smoke-rustfs: ## Storage smoke vs a REAL rustfs S3 backend (round-trip + a pylance table over s3://)
	$(DAGGER) call smoke-rustfs

# The storage-AGNOSTIC proof: the catalog lifecycle e2e with the bytes on a RustFS service bound to one
# exec. Replaced `scripts/rustfs_e2e.sh`, which drove `docker compose up -d --build` over two
# compose files and was invisible to the estate's own docker gate for most of a day — the gate wanted
# `up` on the same line as `compose`, and the script wrapped it.
rustfs-lifecycle: ## Catalog lifecycle e2e with the bytes on RustFS (proves S3-agnosticism)
	$(DAGGER) call rustfs-lifecycle

# The scoped-users hook's provisioning script against a throwaway RustFS that boots as the fixture does (root pair
# from files): a policy the store refuses fails the run before it binds anyone (XC-163).
rustfs-provisioning: ## Scoped-users provisioning vs a REAL RustFS (a refused policy fails the hook, binds nobody)
	$(DAGGER) call rustfs-provisioning

# The dataops loop: authorization + provenance AUTHORSHIP + medallion lineage, with real Dex tokens
# and not one hand-written tuple. Replaced `scripts/governance_e2e.sh`, which layered FOUR compose
# files to assemble the same thing — and which could not have passed as written: it never exported
# `LANCE_E2E_DEX_SECRET`, so its own test sent a client secret to a Dex client both configs declare
# `public: true`, which answers 401.
#
# `DEMO=1 ./scripts/governance_e2e.sh` becomes `dagger call governance-chain --demo` — the narrated
# walkthrough against the SAME stack, because a demo that stands up its own is how a demo drifts from
# what the tests prove.
governance-chain: ## Governance e2e — authz + provenance authorship + lineage (add --demo for the walkthrough)
	$(DAGGER) call governance-chain

# The NATS permission table (`nats.auth.users`) on a real broker: the chart's nats-server in operator mode
# with test-only keys, every bus client driven as its own user and refused on every subject it does not
# own, and real daprd running the chart's Components against it ([[XC-078]]). `dagger call nats-auth
# --values=<file>` reads the table from another values file, e.g. a copy with one allowance deleted.
nats-auth: ## NATS auth e2e — each bus client admitted only to its own subjects (operator-mode broker + daprd)
	$(DAGGER) call nats-auth

# ---- local k3s ------------------------------------------------------------
# An image that is not in $(K3S_IMAGES) is never built by `make k3s-build` and never
# side-loaded by `make k3s-import`, so a hand-built image lands outside CONTAINERD and k3s answers
# ErrImagePull. Measured 2026-08-06: a Dagger build succeeded (exit 0), `kubectl set image` ->
# ErrImagePull, rolled back.
KUBECONFIG ?= /etc/rancher/k3s/k3s.yaml
# Routed through scripts/helm.sh, which sets HELM_DRIVER=sql. NOT bare `helm`: the release lives in
# Postgres, and against the default Secret backend helm reports it ABSENT and `upgrade --install`
# re-installs over a live estate WITHOUT erroring. Read-only subcommands pass straight through.
# The release's namespace, stated rather than inherited from the kubeconfig context ([[XC-152]]): the fixtures it reads
# by name (rask-rustfs, rask-dex, rask-openbao, rask-nats) install into `default` (each fixture.env), so the
# release must share it. Helm reads HELM_NAMESPACE; kubectl takes the flag, and a later `-n`/`-A` in a recipe wins.
RELEASE_NAMESPACE ?= default
HELM ?= KUBECONFIG=$(KUBECONFIG) HELM_NAMESPACE=$(RELEASE_NAMESPACE) ./scripts/helm.sh
KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl --namespace $(RELEASE_NAMESPACE)
# lance-rest-catalog is the ONE lakehouse image (catalog + lineage + maintenance — chart `image.catalog`);
# the default render runs its containers from it, so the
# build/import set must carry it or kind/k3s deploys ImagePullBackOff on every lakehouse pod. web-console is the
# console's static-file image (chart `image.console`, from .docker/frontend.dockerfile), which the release waits on too.
# The object store fixture runs the upstream RustFS image by digest, which the node pulls; nothing here builds it.
K3S_IMAGES = web-console lance-rest-catalog
# Where k3s-build leaves each image as an OCI tarball and k3s-import reads it: no docker daemon on either side.
K3S_IMAGE_DIR ?= build/k3s-images

# Subchart repos (Chart.yaml dependencies). An OCI dependency would need no repo add.
K3S_DEP_REPOS = openfga=https://openfga.github.io/helm-charts

k3s-install: ## One-time host bootstrap: k3s + helm only (everything else is the chart; sudo)
	./scripts/k3s-install.sh

k3s-deps: ## Add subchart repos + vendor chart dependencies into chart/charts/, and each local fixture chart's
	@for r in $(K3S_DEP_REPOS); do n=$${r%%=*}; u=$${r#*=}; helm repo add $$n $$u >/dev/null 2>&1 || true; done
	@helm repo update >/dev/null
	@# Every charts/ directory is gitignored and generated. `helm dependency build` adds the archives Chart.lock names
	@# but never removes others, and helm renders every archive it finds there: a subchart left from an older
	@# Chart.yaml (nvidia-device-plugin, measured 2026-10-12 on a second machine) fails `helm template` after a pull.
	rm -f chart/charts/*.tgz deploy/fixtures/*/chart/charts/*.tgz
	helm dependency build ./chart
	./scripts/fixtures.sh --vendor

k3s-build: ## Build the lakehouse and console images as OCI tarballs in K3S_IMAGE_DIR (via Dagger, no docker)
	@# Each dockerfile stem differs from its image name (frontend -> web-console, rest-catalog ->
	@# lance-rest-catalog), so each is one line. Same builds scripts/e2e_stack.sh does.
	bash scripts/dagger-image.sh --name frontend --out $(K3S_IMAGE_DIR)/web-console.tar
	bash scripts/dagger-image.sh --name rest-catalog --out $(K3S_IMAGE_DIR)/lance-rest-catalog.tar

k3s-import: ## Side-load the k3s-build tarballs into k3s containerd as <image>:dev (sudo, no docker)
	@for s in $(K3S_IMAGES); do \
	  echo ">> importing $$s:dev"; \
	  sudo k3s ctr -n k8s.io images import --digests --index-name docker.io/library/$$s:dev $(K3S_IMAGE_DIR)/$$s.tar || exit 1; \
	done

# The LOCAL dev identity values. auth.enabled is ON in the chart's own defaults (a security default must
# fail closed), and the chart REFUSES to render it without an issuer, so the local loop supplies the dev
# Dex's here. It is not a secret. Override it from the environment for a differently-addressed dev cluster; the
# console's sign-in callbacks are the dex fixture's `console` value.
DEV_ISSUER          ?= http://localhost:8080/dex
# The store address the catalog vends (LH-489): the rustfs fixture's node port on the node's own address, which a pod
# and this host both reach, where `rask-rustfs:9000` resolves only in the cluster.
DEV_NODE_IP         ?= $(shell $(KUBECTL) get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)
DEV_STORE_NODE_PORT := $(shell sed -n 's/^nodePort: *//p' deploy/fixtures/rustfs/chart/values.yaml)
DEV_VENDED_ENDPOINT ?= http://$(DEV_NODE_IP):$(DEV_STORE_NODE_PORT)

# The prerequisites a dev estate gets from deploy/fixtures, each its own release, installed before rask's
# (NOT FOR PRODUCTION; deploy/fixtures/README.md). Dapr's control plane comes first after ESO, so its injector is
# Ready before any pod that wants a sidecar exists; the NATS bus every pub/sub component dials follows. The CloudNativePG
# operator serves the AGE-on-CNPG Cluster; its CRDs come from k3s-crds, which runs first. Dex is the dev OIDC issuer,
# given the issuer rask is given; rustfs and openbao are the dev object and secret stores, which start once
# rask is installed: the object store's root credential and the secret store's seed scripts are rask's; observability is
# the store, dashboards and alerting rask's Collector exports to; marquez is the graph lineage forwards to.
FIXTURES ?= external-secrets dapr nats cloudnative-pg dex rustfs openbao observability marquez

k3s-fixtures: ## Install the development fixtures (FIXTURES) as their own releases, before the rask chart
	$(if $(filter-out dex,$(FIXTURES)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh $(filter-out dex,$(FIXTURES)))
	$(if $(filter dex,$(FIXTURES)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh dex -- --set-string issuer=$(DEV_ISSUER))

k3s-crds: ## Apply the vendored CRDs no release owns (see chart/.helmignore), before the cloudnative-pg fixture
	@# Server-side apply: the CNPG CRD set exceeds kubectl's client-side annotation limit.
	kubectl apply --server-side -f chart/crds-bootstrap/

# THE RELEASE'S VALUES ARE COMPOSED FROM GIT, NEVER READ BACK FROM THE RELEASE ([[XC-194]]). Each upgrade resets to the
# chart's defaults and supplies exactly these: the estate's overlay, where its images come from, the image tags the pins
# file names, and three values derived here. A value that exists only in the live release is dropped by the next
# `make k3s-up`, so the estate is what git declares, and `make k3s-values` prints the composition that
# `helm get values rask -o json` must equal after it.
#
# K3S_REGISTRY is the registry k3s pulls first-party images from: the dev registry `make dev-registry` points k3s at.
# Set it empty for side-loaded images (`make k3s-build k3s-import`, the `dev` tag, `image.localImages`).
K3S_REGISTRY ?= localhost:5000
# The image tags the release runs: written by `make k3s-pins` (captured from the cluster, or set for a new build with
# TAGS=), gitignored, and read only from here. Pulling from a registry needs it, and K3S_PINS_CHECK refuses a file that
# pins no tag for an image the release renders, which would otherwise fall back to the chart's `dev` and wait out
# `--wait` in ImagePullBackOff; side-loaded images run at `dev` when it is absent.
K3S_PINS = deploy/values-live-pins.yaml
K3S_PINS_ARG = $(if $(K3S_REGISTRY),-f $(K3S_PINS),$(if $(wildcard $(K3S_PINS)),-f $(K3S_PINS)))
K3S_PINS_CHECK = $(if $(K3S_REGISTRY),for s in $(K3S_IMAGES); do grep -q "^    $$s: " $(K3S_PINS) 2>/dev/null || { echo "!! $(K3S_PINS) pins no tag for $$s: the release would pull $(K3S_REGISTRY)/$$s:dev. Name the build with make k3s-pins TAGS=\"$$s=<tag>\" (or K3S_REGISTRY= for side-loaded images)" >&2; exit 1; }; done,:)
# The estate admin is the Dex fixture's alice, whose principal is derived from the fixture's own values as the live
# lanes derive theirs (scripts/stack_values.sh). The bootstrap-admin hook grants it `admin` on estate:rask; without it
# nobody can mint a project. Expanded once, and only by a target that composes the values.
DEV_ADMIN_EMAIL ?= alice@example.com
DEV_ADMIN = $(eval DEV_ADMIN := $(shell uv run --no-project --with pyyaml python scripts/dex_principal.py $(DEV_ADMIN_EMAIL)))$(DEV_ADMIN)
K3S_VALUES = --reset-values \
  -f deploy/values-local.yaml \
  $(if $(K3S_REGISTRY),--set-string image.repository=$(K3S_REGISTRY),--set image.localImages=true) \
  $(K3S_PINS_ARG) \
  --set-string auth.oidc.issuer=$(DEV_ISSUER) \
  --set-string vending.endpoint=$(DEV_VENDED_ENDPOINT) \
  --set-string auth.bootstrapAdmin=$(DEV_ADMIN)

k3s-values-check: ## Refuse to compose the release's values when a derived one came out empty
	@case '$(DEV_VENDED_ENDPOINT)' in http://:*|*:) echo "DEV_VENDED_ENDPOINT '$(DEV_VENDED_ENDPOINT)' names no node address or port: is kubectl pointed at the cluster?" >&2; exit 1;; esac
	@case '$(DEV_ADMIN)' in dex~?*) ;; *) echo "the estate admin '$(DEV_ADMIN)' is no Dex principal: scripts/dex_principal.py $(DEV_ADMIN_EMAIL) failed" >&2; exit 1;; esac

k3s-up: k3s-crds k3s-fixtures k3s-deps k3s-stem-check k3s-values-check ## Apply CRDs, install fixtures, vendor deps, then install/upgrade the release from git's values and wait for the catalog
	@$(K3S_PINS_CHECK)
	$(HELM) upgrade --install rask ./chart --wait --wait-for-jobs --timeout 20m --take-ownership $(K3S_VALUES)
	$(KUBECTL) rollout status deploy/rask-catalog --timeout=300s
	@echo "API → http://<node-ip>/api/catalog/livez   (host-less routes on Traefik's Gateway; over VS Code/ssh -L forward port 80 → http://localhost:<port>/)"

k3s-values: k3s-values-check ## Print the values `make k3s-up` supplies, as `helm get values rask -o json` reads them (a server dry run: writes nothing)
	@$(K3S_PINS_CHECK)
	@$(HELM) upgrade --install rask ./chart --dry-run=server -o json $(K3S_VALUES) | jq -S .config

k3s-stem-check: ## Refuse an upgrade while any image stem runs more than one tag ([[LH-169]])
	@# A PREREQUISITE OF k3s-up, not a courtesy target. The chart carries ONE tag per image stem and ten
	@# workloads share `lance-rest-catalog`, so a `kubectl set image` on part of a stem leaves the release
	@# pinning the other half — and the next `helm upgrade`, even one with byte-identical values, silently
	@# reverts whatever was rolled forward. Measured 2026-09-16: six on main-3803cc1d against four on
	@# main-9e5ff5b3, while the image carrying the fixes was deployed nowhere.
	@#
	@# The detection already existed inside `k3s-pins.sh` and was reachable only by asking for a pin file.
	@# Same script, `--check-only`: one implementation, and it now stands in FRONT of the destructive
	@# operation instead of beside it.
	@KUBECONFIG=$(KUBECONFIG) RELEASE_NAMESPACE=$(RELEASE_NAMESPACE) ./scripts/k3s-pins.sh --check-only

k3s-pins: ## Capture what the cluster is RUNNING into deploy/values-live-pins.yaml (#135), or set TAGS="<stem>=<tag> ..." in it
	@# KUBECTL is deliberately NOT passed through. `KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl` is a
	@# multi-word string carrying its own env assignment, so `KUBECTL=$(KUBECTL) ./script` made the shell
	@# read `KUBECTL=KUBECONFIG=...` as an assignment and then run `kubectl ./scripts/k3s-pins.sh` —
	@# `error: unknown command "./scripts/k3s-pins.sh" for "kubectl"`. The script quotes `"$$KUBECTL"`,
	@# so passing it correctly would fail too: it would exec a binary literally named
	@# `KUBECONFIG=... kubectl`. KUBECONFIG is already in the environment; the script's own `kubectl`
	@# default is the right one.
	@# TAGS="<stem>=<tag> ..." sets those stems' tags in the file and reads no cluster: how a new build reaches the release.
	@KUBECONFIG=$(KUBECONFIG) RELEASE_NAMESPACE=$(RELEASE_NAMESPACE) ./scripts/k3s-pins.sh $(foreach t,$(TAGS),--tag $(t)) $(K3S_PINS)
	@echo ">> now: make k3s-up"

# Make expands a recipe's lines before running the first, so `$(wildcard)` would look for the pins file before this
# recipe writes it: the converge names it unconditionally.
k3s-converge: K3S_PINS_ARG = -f $(K3S_PINS)
k3s-converge: k3s-values-check ## Roll a WHOLE image stem to one tag and upgrade (the resolution k3s-stem-check names). STEM=lance-rest-catalog TAG=main-<sha>
	@# THE DOCUMENTED FIX FOR A SPLIT STEM, made runnable. `k3s-stem-check` refuses `k3s-up` while one
	@# stem runs several tags and tells the operator to "rebuild one image from a commit carrying every
	@# change, roll the whole stem, re-run" — but the rolling half had no command, so it was done by
	@# hand with `kubectl set image` per deployment. That is the SECOND WRITER the chart's own comments
	@# warn about: a partial roll leaves the release pinning the other half, and the next `helm upgrade`
	@# silently reverts it.
	@#
	@# One helm upgrade instead, which converges every workload on the stem in a single transaction and
	@# leaves the release agreeing with the cluster. It deliberately does NOT depend on k3s-stem-check:
	@# this target is what RESOLVES the split the check refuses to proceed through. The tag is written into the pins
	@# file first and the values are k3s-up's, so the next `make k3s-up` deploys the same tag.
	@test -n "$(TAG)" || { echo "!! TAG is required, e.g. make k3s-converge TAG=main-$$(git rev-parse --short=8 HEAD)"; exit 2; }
	@./scripts/k3s-pins.sh --tag $(or $(STEM),lance-rest-catalog)=$(TAG) $(K3S_PINS)
	@$(K3S_PINS_CHECK)
	$(HELM) upgrade --install rask ./chart --wait --wait-for-jobs --timeout 20m --take-ownership $(K3S_VALUES)
	@# SINGLE QUOTES around the suggested command, never backticks: a backtick inside an echo is
	@# COMMAND SUBSTITUTION, so `>> ... `make k3s-up` ...` RAN a full k3s-up as a side effect of
	@# printing advice. Observed 2026-09-22 in this target's own output — "every stem converged —
	@# make[1]: Entering directory" — after which the stem check printed twice because k3s-up ran its
	@# own. Harmless only because k3s-up is idempotent.
	@# EVIDENCE, NOT A GATE — and the difference decides the exit code. The upgrade above already
	@# happened; `--check-only` reads EVERY stem, so an unrelated one being split would fail this
	@# target after a deploy that succeeded, which reads as "the deploy failed". It reports instead,
	@# and says what is still true so the operator's next move is obvious.
	@if KUBECONFIG=$(KUBECONFIG) ./scripts/k3s-pins.sh --check-only; then \
	  echo ">> every stem converged — 'make k3s-up' will now pass its own check"; \
	else \
	  echo ">> this deploy SUCCEEDED, but another stem is still split (named above)."; \
	  echo ">> 'make k3s-up' will keep refusing until that one is converged too — same fix, different TAG/STEM."; \
	fi

k9s: bootstrap ## Browse the k3s cluster in k9s (the chart's NOTES.txt points here)
	@KUBECONFIG=$(KUBECONFIG) $(LOCALBIN)/k9s

k3s-down: ## Uninstall the rask release (keep PVCs)
	$(HELM) uninstall rask --ignore-not-found --wait

k3s-purge: k3s-down ## Uninstall + delete PVCs (clean slate)
	$(KUBECTL) delete pvc -A -l app.kubernetes.io/instance=rask --ignore-not-found || true
	# Stale PVCs left in an old per-component `nats` namespace (NATS runs in the release namespace) that the
	# label-scoped delete above misses. dapr-system is the dapr FIXTURE's, which k3s-down leaves installed.
	-$(KUBECTL) delete pvc -n nats --all --ignore-not-found

# ---- kind (throwaway CI-shaped cluster; k3s above is the long-lived local deploy) ----
# Same chart, same :dev image set, same release name (rask) as k3s — but on a disposable
# kind cluster, which is what the CI live-proof job (e2e-stack) boots. `make e2e-ci` (scripts/e2e_stack.sh) is the one
# path onto it: it builds the image set as Dagger tarballs, loads them with `kind load image-archive` and installs the
# fixtures and the chart with the values the lane renders; `kind-up` and `kind-down` only create and delete the cluster.
# Toolchain is pinned into .localbin by `make bootstrap` (kind/kubectl/fga; helm + docker from PATH).
.PHONY: bootstrap kind-up kind-down e2e-ci e2e-isolation e2e-container-deletes e2e-fga-model

LOCALBIN     := $(CURDIR)/.localbin
KIND         := $(LOCALBIN)/kind
KIND_CLUSTER := rask
# OS/arch detection so bootstrap works on Linux/macOS, x86_64/arm64. sed/tr, NOT a shell
# `case`: a `)` inside $(shell …) prematurely closes make's paren-match.
HOST_OS   := $(shell uname -s | tr '[:upper:]' '[:lower:]')
HOST_ARCH := $(shell uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
KIND_V    := v0.25.0
KUBECTL_V := v1.31.3
FGA_V     := 0.6.4
K9S_V     := v0.32.7
HELM_DOCS_V := 1.14.2
# helm-values-schema-json's standalone `schema` binary: chart/values.schema.json from values.yaml (scripts/chart_docs.sh).
HELM_SCHEMA_V := 2.6.0
# helm-docs names its assets Linux/Darwin and x86_64/arm64.
HELM_DOCS_ARCH := $(shell uname -m | sed -e 's/aarch64/arm64/')
# k9s tags its assets Linux/Darwin (capitalised), unlike everything else above.
K9S_OS    := $(shell uname -s)

bootstrap: ## Download kind/kubectl/fga/k9s/helm-docs/helm-schema into .localbin (idempotent) — helm + docker must be on PATH
	@mkdir -p $(LOCALBIN)
	@test -n "$(HOST_ARCH)" || { echo "!! unsupported CPU '$$(uname -m)' — need x86_64 or arm64"; exit 1; }
	@test -x $(KIND)              || { echo "kind ->"; curl -fsSL -o $(KIND) "https://kind.sigs.k8s.io/dl/$(KIND_V)/kind-$(HOST_OS)-$(HOST_ARCH)" && chmod +x $(KIND); }
	@test -x $(LOCALBIN)/kubectl  || { echo "kubectl ->"; curl -fsSL -o $(LOCALBIN)/kubectl "https://dl.k8s.io/release/$(KUBECTL_V)/bin/$(HOST_OS)/$(HOST_ARCH)/kubectl" && chmod +x $(LOCALBIN)/kubectl; }
	@test -x $(LOCALBIN)/fga      || { echo "fga ->"; curl -fsSL "https://github.com/openfga/cli/releases/download/v$(FGA_V)/fga_$(FGA_V)_$(HOST_OS)_$(HOST_ARCH).tar.gz" | tar xz -C $(LOCALBIN) fga; }
	@test -x $(LOCALBIN)/k9s      || { echo "k9s ->"; curl -fsSL "https://github.com/derailed/k9s/releases/download/$(K9S_V)/k9s_$(K9S_OS)_$(HOST_ARCH).tar.gz" | tar xz -C $(LOCALBIN) k9s; }
	@test -x $(LOCALBIN)/helm-docs || { echo "helm-docs ->"; curl -fsSL "https://github.com/norwoodj/helm-docs/releases/download/v$(HELM_DOCS_V)/helm-docs_$(HELM_DOCS_V)_$(K9S_OS)_$(HELM_DOCS_ARCH).tar.gz" | tar xz -C $(LOCALBIN) helm-docs; }
	@test -x $(LOCALBIN)/helm-schema || { echo "helm-schema ->"; curl -fsSL "https://github.com/losisin/helm-values-schema-json/releases/download/v$(HELM_SCHEMA_V)/helm-values-schema-json_$(HELM_SCHEMA_V)_$(HOST_OS)_$(HOST_ARCH).tgz" | tar xz -C $(LOCALBIN) schema && mv $(LOCALBIN)/schema $(LOCALBIN)/helm-schema; }
	@command -v docker >/dev/null || { echo "!! docker not on PATH — install https://docs.docker.com/get-docker/"; exit 1; }
	@command -v helm   >/dev/null || { echo "!! helm not on PATH — install https://helm.sh/docs/intro/install/"; exit 1; }
	@echo "toolchain ready in .localbin ($(HOST_OS)/$(HOST_ARCH))"

kind-up: bootstrap ## Create the rask kind cluster (idempotent; deploy/kind/kind-config.yaml)
	@$(KIND) get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
	  $(KIND) create cluster --name $(KIND_CLUSTER) --config deploy/kind/kind-config.yaml --wait 150s

kind-down: ## Delete the rask kind cluster
	$(KIND) delete cluster --name $(KIND_CLUSTER)

# THE guarded live proof — identical to the CI `e2e-stack` job (both shell out
# to the same script, so "green in CI" and "green on my machine" cannot diverge). The
# scripts bring up their own governed kind stack, seed grants/buckets, run the suites, and
# (in CI) tear the cluster down.
e2e-live: ## Run the e2e suites against the DEPLOYED k3s release (discovers addresses + credentials from the cluster)
	@bash scripts/e2e_live.sh $(ARGS)

e2e-ci: bootstrap ## Governed kind stack + every tests/e2e-py module it can serve, then Phase 1's acceptance and its soak == CI e2e-stack
	CLUSTER=$(KIND_CLUSTER) RELEASE=rask bash scripts/e2e_stack.sh

# The tenant-isolation attack (#74's live half), as a STANDALONE target against an already-deployed
# estate. `e2e-ci`'s kind stack now runs the same suite itself: `scripts/e2e_stack.sh` provisions a
# second project and grants bob admin on it, so the five legs execute there under the harness's
# no-silent-skips gate. This target remains for driving the suite against a cluster the harness did
# not build, where the two tokens come from the operator rather than from Dex-on-localhost.
# The suite's own docstring cited this target for months while no Makefile defined it (diff2 F5) —
# so anyone following the docs ran nothing and saw no error.
#
# Un-skipping it is the point: with the env unset every test in the file SKIPS, which is why a
# key-name mismatch in it survived to be found by reading rather than by running.
e2e-isolation: ## Cross-tenant credential attack vs a deployed vending-enabled stack (needs 2 tenants)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! set LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_TENANT_B_TOKEN, LANCE_E2E_PROJECT_B (and LANCE_E2E_S3)"; exit 1; }
	uv run pytest tests/e2e-py/test_credential_isolation_e2e.py -m e2e -v

# The CONTAINER tier's deletes ([[LH-028]]): table-level drop/protect/force/undrop were proven live and
# warehouse/project delete never were, which is exactly where `force` and cascade interact. Every
# container the suite destroys is one it created, and it never sends `purge_bucket` — the rule worth
# pinning is the refusal, and a customer's bucket is not recoverable.
#
# The disclosure leg needs an identity that administers NOTHING here, and `scripts/e2e_live.sh` is what
# picks one: bob is in acme's admins role, which administers `project:acme`, so he holds `can_administer` and would make
# that leg allege a property the estate does not have. Prefer `bash scripts/e2e_live.sh <file>`, which
# discovers the whole environment from the running release.
e2e-fga-model: ## The DEPLOYED OpenFGA model matches this repo's — the third copy `fga-test` cannot see
	@# `make fga-test` diffs model.json against model.fga. The store is the third copy and was checked by
	@# nothing, which is how it fell NINE relations behind without a test going red — until a Helm hook
	@# CrashLooped writing a tuple naming one of them and wedged the release ([[LH-174]]). The write is now
	@# a chart hook; this is the other half, because a hook that writes and a check that the write LANDED
	@# are different assertions and only the second notices a hook that silently stopped running.
	@test -n "$(LANCE_E2E_FGA)" || { echo "  !! set LANCE_E2E_FGA (a deployed OpenFGA), or run: bash scripts/e2e_live.sh tests/e2e-py/test_the_deployed_model_is_the_one_the_code_reasons_about.py"; exit 1; }
	uv run pytest tests/e2e-py/test_the_deployed_model_is_the_one_the_code_reasons_about.py -m e2e -v

e2e-container-deletes: ## Warehouse/project delete, cascade and the gate-before-disclosure rule, vs a deployed release
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! set LANCE_E2E_CATALOG_URL + LANCE_E2E_TOKEN (and LANCE_E2E_NONADMIN_TOKEN for the disclosure leg), or run: bash scripts/e2e_live.sh tests/e2e-py/test_the_container_tier_deletes_are_driven.py"; exit 1; }
	uv run pytest tests/e2e-py/test_the_container_tier_deletes_are_driven.py -m e2e -v

# ---- one target per e2e suite marker (audit H1) ------------------------------
# pyproject.toml declares twelve per-suite markers with the comment "used by the e2e make targets
# (e.g. `pytest -m media`)". NONE of them was selected by any target, script or CI job — measured:
# `grep -rnE '\-m ["']?(cas|compaction|…)' Makefile scripts .github .dagger` returned nothing. So
# eleven live suites ran in no lane at all, and the declarations read as if they drove something.
#
# Every marker has exactly one invocation site, and nothing offline checks it: a change that deletes a
# suite deletes its marker, its target and its `E2E_SUITES` entry with it.
#
# All of them need a live deployed stack (that is what `e2e` means here); they are runnable proofs,
# not a CI lane. `make e2e-ci` remains the governed-kind-stack entry point, and wiring the
# security-shaped ones (edge, cas) into CI is the follow-up — it needs an edit to
# `.github/workflows/ci.yml`, which a concurrent session is holding.
E2E_SUITES = byo-branch byo-erasure byo-pipeline cas chaos compaction edge observability phase1 soak track-a
.PHONY: $(addprefix e2e-,$(E2E_SUITES))   # declared HERE, not up with the other .PHONY: make
                                          # expands a rule's prerequisites AS IT READS the line,
                                          # so referencing E2E_SUITES before this assignment
                                          # expands to nothing and silently declares no target.

e2e-byo-branch:     ## LH-490 live proof: a branch the owner creates through CreateTableBranch takes a writer's stock append, delete and update through version/create on the table's vended credential, main is unchanged, a refused branch commit answers a typed error, and the store refuses the credential a final manifest and a ref (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_a_stock_client_commits_on_a_branch.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-branch needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-branch needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_branch -v --require-live
e2e-byo-erasure:    ## LH-493 live proof: the erasure door records an obligation and commits nothing, a writer+maintainer job rewrites through version/create, a skipped compaction stays named, and the catalog releases what it verified (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_an_erasure_is_rewritten_by_a_bring_your_own_job.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_erasure -v --require-live
e2e-byo-pipeline:   ## LH-478 live proof: a stock-client pipeline seeds bronze, derives silver with provenance, is held behind `published`, approved and followed on /events?since= (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_pipeline -v --require-live
e2e-cas:            ## Object-store + registry conditional-write (CAS) proofs (needs LANCE_E2E_S3_ENDPOINT)
	@test -n "$(LANCE_E2E_S3_ENDPOINT)" || { echo "  !! e2e-cas needs LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_S3_ENDPOINT=$(LANCE_E2E_S3_ENDPOINT) uv run pytest tests/e2e-py -m cas -v
e2e-chaos:          ## Criterion 5's dependency-outage drills (XC-095): each takes one dependency of the release down, drives the doors while it is gone, restores it and holds the estate to criteria 1 and 4; they TAKE THE RELEASE'S DEPENDENCIES DOWN, so they run only with --chaos (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on its cluster; `bash scripts/e2e_live.sh -m phase1 --require-live --chaos` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_S3=$(LANCE_E2E_S3) uv run pytest tests/e2e-py -m chaos -v --require-live --chaos
e2e-compaction:     ## Maintenance sweep proofs: the deployed service's sweep and the in-process sweep on real S3 (needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT with its LANCE_E2E_S3_ACCESS_KEY and LANCE_E2E_S3_SECRET_KEY)
	@test -n "$(LANCE_E2E_MAINTENANCE_URL)" || { echo "  !! e2e-compaction needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT, LANCE_E2E_S3_ACCESS_KEY, LANCE_E2E_S3_SECRET_KEY — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DAPR_TOKEN)" || { echo "  !! e2e-compaction needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT, LANCE_E2E_S3_ACCESS_KEY, LANCE_E2E_S3_SECRET_KEY — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3_ENDPOINT)" || { echo "  !! e2e-compaction needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT, LANCE_E2E_S3_ACCESS_KEY, LANCE_E2E_S3_SECRET_KEY — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3_ACCESS_KEY)" || { echo "  !! e2e-compaction needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT, LANCE_E2E_S3_ACCESS_KEY, LANCE_E2E_S3_SECRET_KEY — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3_SECRET_KEY)" || { echo "  !! e2e-compaction needs LANCE_E2E_MAINTENANCE_URL, LANCE_E2E_DAPR_TOKEN, LANCE_E2E_S3_ENDPOINT, LANCE_E2E_S3_ACCESS_KEY, LANCE_E2E_S3_SECRET_KEY — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_MAINTENANCE_URL=$(LANCE_E2E_MAINTENANCE_URL) LANCE_E2E_DAPR_TOKEN=$(LANCE_E2E_DAPR_TOKEN) LANCE_E2E_S3_ENDPOINT=$(LANCE_E2E_S3_ENDPOINT) LANCE_E2E_S3_ACCESS_KEY=$(LANCE_E2E_S3_ACCESS_KEY) LANCE_E2E_S3_SECRET_KEY=$(LANCE_E2E_S3_SECRET_KEY) uv run pytest tests/e2e-py -m compaction -v --require-live
# THE ITERATION LOOP, and the measurement that made it a target. `tests/unit` + `tests/integration` is
# what every change runs before it is pushed, and it took 9m57s serially on a 64-core machine. Measured
# 2026-09-19 at `-n 16 --dist loadfile`: **1m17s, 4,947 passed, zero failures** — 7.7x, because the
# suite is a broad front with no hotspot (the slowest single test is 14.1s).
#
# `--dist loadfile` and not the default: three suites are parallel-unsafe at the FILE level (the
# `lance.audit` process-global logger, `configure_audit`'s level, the registry CAS markers), and
# keeping a file's tests on one worker is what makes them safe. [[XC-037]].
#
# `-n 16` rather than `auto`: `auto` takes all 64 cores and the run is IO-bound long before that —
# measured, 16 workers already reach the wall-clock floor while leaving the machine usable.
check-fast: $(OPENFGA_BIN) ## The pre-push suite in parallel — 1m17s against 9m57s serial
	uv run pytest tests/unit tests/integration -q -n 16 --dist loadfile

# REQUIRES its targets, rather than skipping into a green: every test in this suite skips without
# RASK_E2E_EDGE_URL, the other-audience cases without LANCE_E2E_DEX, and a live drive with no live target is a failed
# invocation, not a pass.
e2e-edge:           ## Each /api prefix reaches its service through the cluster Gateway and is refused there anonymously or for another audience (needs RASK_E2E_EDGE_URL, LANCE_E2E_DEX)
	@test -n "$(RASK_E2E_EDGE_URL)" || { echo "set RASK_E2E_EDGE_URL, e.g. make e2e-edge RASK_E2E_EDGE_URL=http://10.16.51.55 LANCE_E2E_DEX=http://localhost:8080/dex"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "set LANCE_E2E_DEX, the Dex the estate's issuer names, e.g. http://localhost:8080/dex"; exit 1; }
	RASK_E2E_EDGE_URL=$(RASK_E2E_EDGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m edge -v --require-live
e2e-observability:  ## OTLP → GreptimeDB observability proof (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_GREPTIME_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_GREPTIME_URL=$(LANCE_E2E_GREPTIME_URL) uv run pytest tests/e2e-py -m observability -v
e2e-phase1:         ## Phase 1's acceptance (XC-090): one drive of the bring-your-own scenario, its own claims, and each criterion module read off it; criterion 1, provenance, is XC-091, criterion 2, the catalog, is XC-092, criterion 4, events, is XC-094, and criterion 5's drills (XC-095) run only with --chaos (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE; `bash scripts/e2e_live.sh -m phase1 --require-live` discovers them, and with `--chaos --keep-drive PATH` runs the drills and keeps the drive for `make e2e-soak`)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_SERVICE_TOKEN_FILE)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_SERVICE_TOKEN_FILE=$(LANCE_E2E_SERVICE_TOKEN_FILE) uv run pytest tests/e2e-py -m phase1 -v --require-live
e2e-soak:           ## Phase 1's soak (XC-090): a day after an acceptance run kept its drive (`--keep-drive PATH`), the estate still reads it as the criteria did, nothing parked and both outboxes drained; removes the kept tenant once it passes (needs SOAK, the kept drive's PATH, and LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 with its key pair; `bash scripts/e2e_live.sh -m soak --require-live --soak PATH` discovers them)
	@test -n "$(SOAK)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_S3=$(LANCE_E2E_S3) uv run pytest tests/e2e-py -m soak -v --require-live --soak $(SOAK)
e2e-track-a:        ## Track A acceptance: tier contract + gate/publish symmetry + branch isolation (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_TOKEN)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3_ENDPOINT)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_TOKEN=$(LANCE_E2E_TOKEN) LANCE_E2E_S3_ENDPOINT=$(LANCE_E2E_S3_ENDPOINT) uv run pytest tests/e2e-py -m track_a -v

# ---- the local image path (registry + Dagger engine) ------------------------
# `make dev-registry` once per host (a local registry on :5000, and k3s pointed at it), and
# `make dagger-engine` once (an engine that may push to a plain-HTTP registry). Every
# `dagger call image ... publish` goes through both. Iterate the console with `make dev` (local Vite HMR); backend
# changes rebuild and `make k3s-up`.
dagger-engine: ## One-time: a Dagger engine that can push to the plain-HTTP dev registry
	@bash scripts/dagger-engine.sh

# ---- reclaiming the dev loop's disk -----------------------------------------
# Both of these grow WITHOUT BOUND if nobody looks, and they grew to ~1.2 TB before anyone did:
#   * the Dagger engine cache — its default GC ceiling is proportional to the disk (5.67 TB on a 7.4 TB
#     volume), so it never collected. `make dagger-engine` now writes an explicit 60 GB cap; this target
#     is the manual sweep.
#     old ones (measured: 113 tags of one web image, 83.9 GB).
# Both are safe: every artefact is reproducible with `dagger call image ... publish`, and k3s
# holds running images in its own containerd cache.
registry-gc: ## Drop all but the newest tags in the dev registry, then sweep blobs
	@bash scripts/registry-gc.sh

dagger-gc: ## Prune the Dagger engine's build cache to its configured ceiling
	@$(DAGGER) core engine local-cache prune

dev-gc: registry-gc dagger-gc ## Reclaim both the registry and the Dagger cache
	@df -h / | tail -1 | awk '{print ">> disk: "$$4" free, "$$5" used"}'

dev-registry: ## One-time: local image registry + point k3s at it (sudo; restarts k3s)
	bash scripts/k3s-registry.sh
