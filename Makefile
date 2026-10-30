.PHONY: k3s-clients fga-store-check fga-estate-migrate sbom zone-sbom registry-gc dagger-gc dev-gc help install build test test-slow lint lint-imports fmt clean storybook typecheck knip comment-gate check coverage fga-test ci dev-micro dev-frontends dev-frontends-k3s dev-zone home frontend-build frontend-check sync-favicons ray-up ray-down ray-status serve-up serve-down serve-status harvest-ead claude-bootstrap ray-up-htr serve-up-both qwen-serve k3s-install k3s-fixtures k3s-deps k3s-build k3s-import k3s-up k3s-down k3s-purge k9s bootstrap dev-registry e2e frontend-images prod-render-check chart-docs chart-docs-check alert-rules-check alert-rules-drill notifications-lanes notifications-rig audit smoke-rustfs rustfs-lifecycle rustfs-provisioning auth-chain governance-chain nats-auth go-fmt scan-config scan-secrets scan-image scan-zone-image seed-corpus e2e-isolation e2e-container-deletes e2e-fga-model

help:
	@echo "Targets:"
	@echo "  install build test lint fmt clean storybook"
	@echo "  typecheck knip check ci   frontend-check frontend-build"
	@echo "  dev-micro                              — backend fleet (per-domain services; no edge)"
	@echo "  dev-frontends                          — all 6 zones behind the :3024 proxy (browse http://localhost:3024)"
	@echo "  dev-frontends-k3s                      — same, but /api → the IN-CLUSTER edge (Traefik, port-forwarded)"
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

# Python tests via pytest. THE FRONTENDS DO HAVE A UNIT SUITE and this line used to deny it: 128
# tracked vitest files across the 18 packages that declare a `test` script. `make test` does not run
# them — `bun --cwd=frontend run test` does, and CI runs both — so the honest statement is that this
# target covers ONE plane, not that the other plane has nothing to cover.
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

# The sealed runners' suites. A runner is sealed OUT of the root workspace (own lock, own venv), so the
# root pytest can neither import nor collect its tests: each suite runs from INSIDE its own directory,
# where pytest reads that runner's config and `uv run --frozen` syncs from that runner's lock. From the
# repo root pytest would read the ROOT testpaths and die at collection on fleet modules the runner's
# venv lacks. The loop covers every `runners/*/tests`, so a runner that gains a suite runs in both
# `make test` and `make test-slow` with no edit here, and `--frozen` fails loudly on a runner that has
# a suite but commits no uv.lock. `|| exit 1` because a shell loop reports only its LAST iteration's
# status: without it a failing suite followed by a passing one reads as green.
RUNNER_SUITES := $(sort $(patsubst %/tests,%,$(wildcard runners/*/tests)))
run-runner-suites = for runner in $(RUNNER_SUITES); do (cd $$runner && uv run --frozen pytest $(1)) || exit 1; done

# The OpenFGA server the offline suite asserts authorization outcomes against (root `conftest.py::openfga`, [[LH-235]]):
# the static binary out of the very image `.dagger/governed.go` pins and `dagger call test` copies onto PATH, so a
# developer box and CI evaluate the model with one server. It depends on that file, so a bumped pin re-exports it.
OPENFGA_BIN := $(CURDIR)/.localbin/openfga
OPENFGA_IMAGE = $(shell sed -n 's/^[[:space:]]*openfgaImage[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' .dagger/governed.go)
$(OPENFGA_BIN): .dagger/governed.go
	@test -n "$(OPENFGA_IMAGE)" || { echo "!! no openfgaImage pin read from .dagger/governed.go"; exit 1; }
	@mkdir -p $(dir $@)
	dagger core container from --address=$(OPENFGA_IMAGE) file --path=/openfga export --path=$@
	@chmod +x $@ && touch $@
.PHONY: openfga
openfga: $(OPENFGA_BIN) ## Export the pinned OpenFGA server binary to .localbin, for the suite's authorization tests

test: $(OPENFGA_BIN)
	uv run pytest -m "not slow and not e2e" $(PYTEST_PARALLEL)
	$(call run-runner-suites,-m "not slow")

# Slow tests need real models / a GPU (e.g. the YOLO layout smoke test) and hang on hosts without
# them, so this is opt-in. The root workspace declares no `slow` mark at all: every one lives in a
# sealed runner, so the runner loop, run with no marker filter, is what this target adds over
# `make test`.
test-slow: $(OPENFGA_BIN)
	uv run pytest -m "not e2e" $(PYTEST_PARALLEL)
	$(call run-runner-suites,)

lint:
	uv run ruff check .
	bun --cwd=frontend run lint

# The import-layer contracts in `.importlinter`. `uv run --with` layers the pinned import-linter over
# the workspace venv, so the graph it walks is the workspace's own editable installs.
lint-imports:
	uv run --with import-linter==2.15 lint-imports

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
	rm -rf .venv node_modules **/node_modules **/dist **/.svelte-kit **/.turbo **/storybook-static

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
# prod_render_check.sh's header says "Run: `make prod-render-check`", and deploy/alerting/
# rules_test.yml's says this target runs check-then-test. Only the wrappers were missing.
#
# Bare `helm`/`promtool` on purpose: the Dagger container curls both to /usr/local/bin and
# deliberately excludes .localbin so a host-downloaded binary cannot shadow the pinned one.
prod-render-check: ## Render values-prod.yaml and assert its HA + security switches are ON
	./scripts/prod_render_check.sh

# ---- the notification channel verify rig (open_notifications.md §7) --------
# A SIDE STACK, not a deploy path. It answers the one question unit tests cannot: does exactly ONE
# message leave the estate per (event, subject, channel) when the broker redelivers? Mailpit shows
# every message it received, so a duplicate is visible rather than inferred. Point the email
# binding's host at localhost:1025 while it is up.
# The notification-lane drives. Two tracked, documented .mjs files proving claims no unit test can
# make — that a run whose AUTHOR is not you still reaches you because you are named as the human the
# work is for — and until 2026-08-22 they were referenced by NOTHING: one grep hit each, their own
# declaration. They also sit outside playwright's `testDir: './tests'`, so `make e2e` skipped them too.
#
# A drive nobody can name is a drive nobody runs, and these are the only proof the estate has for the
# ORIGINATOR targeting source end to end. They need a DEPLOYED estate (a live Dapr actor
# plane), which is why they are a named target rather than part of an offline gate.
#
# Pinned by tests/unit/test_e2e_collection_gate.py, which now requires every tests/e2e/verify_*.mjs to
# have an invocation site — so a new drive cannot land unreferenced.
notifications-lanes: ## The notification-lane drives vs a deployed estate (ORIGIN=http://…)
	@test -n "$(ORIGIN)" || { echo "set ORIGIN, e.g. make notifications-lanes ORIGIN=http://localhost:8080"; exit 1; }
	@# OpenFGA admits only a projected rask-openfga token (XC-077): one TokenRequest for the bootstrap Job account.
	@FGA_TOKEN_FILE="$$(mktemp)"; trap 'rm -f "$$FGA_TOKEN_FILE"' EXIT; \
	kubectl create token "$${RELEASE:-rask}-sa-jobs" --audience rask-openfga --duration 1h > "$$FGA_TOKEN_FILE" || exit 1; \
	export FGA_TOKEN_FILE; \
	for drive in tests/e2e/verify_originator_lane.mjs \
	              tests/e2e/verify_notifications_two_users.mjs; do \
		echo "== $$drive"; ORIGIN=$(ORIGIN) node "$$drive" || exit 1; \
	done

# Foreground is right for THIS rig specifically: it is a thing you poke at (open the Mailpit UI,
# redeliver, look again), so seeing it run and killing it with Ctrl-C is the workflow
# rather than a cost. The rustfs smoke went the other way and became a single non-interactive
# `dagger call smoke-rustfs`, because it is a script with an answer.
notifications-rig: ## Mailpit for the channel idempotency drive (Ctrl-C stops it)
	@echo "Mailpit UI  : http://localhost:8025"
	@dagger call notifications-mailpit up --ports=1025:1025 --ports=8025:8025

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

# ---- frontend dead-code + dep gate (knip, repo-wide; see knip.json) ---------
# Cross-workspace tool — analyses the whole JS graph at once, so it stays a
# root-level gate, not a per-package turbo task (lint/fmt ARE per-package turbo tasks).
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
# audit       osv-scanner over all SIX lockfiles + .dagger/go.mod. No build, seconds.
# scan-config trivy misconfig over .docker/ + chart/, and secret detection repo-wide. No build.
# scan-image  trivy over an image DAGGER BUILT — needs the build first, so it is minutes, not seconds.
#             NAME is the .docker/<stem>.dockerfile stem; ZONE is a frontend/microfrontends/ dir.
audit:
	dagger call audit

# The Go plane's formatting gate. `.dagger/` implements every CI gate in this estate and nothing
# checked its own formatting — `gofmt -l` reported two tracked hand-written files unformatted. Same
# contract as `audit` above: one definition, `make go-fmt` == `dagger call go-fmt`.
#
# It runs in a container because there is NO Go toolchain on the developer PATH here, and the
# repository rule is absolute: every container goes through Dagger. `go-fmt-fixed` is the write half —
# `dagger call go-fmt-fixed export --path=.dagger` — because a gate with no paired fixer is a gate
# people work around, and here the only alternative would be installing a toolchain.
go-fmt: ## gofmt gate over the .dagger Go plane (fix: dagger call go-fmt-fixed export --path=.dagger)
	dagger call go-fmt

scan-config:
	dagger call scan-config

# scan-secrets reads the full GIT HISTORY, not the working tree — a credential committed and later
# deleted is still public on a public repo. Gates on VERIFIED results only (trufflehog asks the
# provider whether the key works), so it blocks in CI where the other two legs are report-only.
scan-secrets:
	dagger call scan-secrets

scan-image:
	@test -n "$(NAME)" || { echo "  !! usage: make scan-image NAME=notifications"; exit 1; }
	dagger call scan-image --name=$(NAME)

# THE INVENTORY, not a gate. `scan-image` answers "is this image clean today"; this answers "what is
# IN it", which is the question a new CVE asks and the one nothing here could answer. Emitted from the
# same tarball the scanner reads, so the SBOM describes the exact bytes that were scanned.
sbom: ## CycloneDX SBOM for one image (NAME=notifications) — writes ./sbom-<name>.cdx.json
	@test -n "$(NAME)" || { echo "  !! usage: make sbom NAME=notifications"; exit 1; }
	dagger call sbom --name=$(NAME) export --path=./sbom-$(NAME).cdx.json
	@echo "  -> ./sbom-$(NAME).cdx.json"

zone-sbom: ## CycloneDX SBOM for one micro-frontend zone (ZONE=lakehouse)
	@test -n "$(ZONE)" || { echo "  !! usage: make zone-sbom ZONE=lakehouse"; exit 1; }
	dagger call zone-sbom --zone=$(ZONE) export --path=./sbom-web-$(ZONE).cdx.json
	@echo "  -> ./sbom-web-$(ZONE).cdx.json"

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
# library's watcher, orchestrated by Turborepo. Each zone's server reaches /api/* at
# RASK_EDGE_URL and its Vite dev server proxies /api/* to VIEWER_BACKEND (both :8888,
# an edge — `make dev-frontends-k3s` forwards the cluster's). The zones come up on
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
ZONES ?= home lakehouse explorer annotator compute studio models

dev-frontends:        # build the ui + api libs once, then all zones + :3024 proxy
	# Build the libs FIRST so the zones read a complete dist/. Running `turbo run dev`
	# unfiltered also starts the ui library's `svelte-package -w` watcher, which rewrites
	# dist/ concurrently and races the zones reading it (one zone crashes → turbo tears
	# the whole run down). Zones-only dev avoids that. To live-edit the ui library, run
	# its watcher in a second terminal: `bun run dev:ui`.
	bunx turbo --cwd=frontend run build --filter='./packages/ui' --filter='./packages/api'
	bunx turbo --cwd=frontend run dev --filter='./microfrontends/*'

dev-frontends-k3s:    # frontend HMR (Path A) against the IN-CLUSTER backend
	# Port-forwards the in-cluster edge (k3s Traefik's Gateway, which serves the chart's
	# HTTPRoutes) to :8888, then runs all zones with Vite HMR pointed at it (VIEWER_BACKEND
	# client proxy + RASK_EDGE_URL SSR, both :8888). One Ctrl-C tears down both (trap kills
	# the port-forward). Needs the cluster up (`make k3s-up`). Browse http://localhost:3024.
	@echo "==> port-forward kube-system/traefik → http://localhost:8888 (Ctrl-C stops both)"
	@$(KUBECTL) -n kube-system port-forward svc/traefik 8888:80 >/dev/null 2>&1 & \
	  PF=$$!; trap 'kill $$PF 2>/dev/null' EXIT INT TERM; \
	  until curl -sf http://localhost:8888/api/ray/health >/dev/null 2>&1; do sleep 1; done; \
	  echo "==> edge reachable; starting frontends (Vite HMR)"; \
	  bunx turbo --cwd=frontend run build --filter='./packages/ui' --filter='./packages/api' && \
	  VIEWER_BACKEND=http://localhost:8888 RASK_EDGE_URL=http://localhost:8888 \
	    bunx turbo --cwd=frontend run dev --filter='./microfrontends/*'

home:      # catch-all zone only, :5273 (serves /)
	bun --cwd=frontend run dev:home

frontend-%:           # run one domain zone on its own port, e.g. `make frontend-explorer`
	bun --cwd=frontend run dev:$*

# ONE zone + its FAKE upstreams — the only frontend loop that needs no cluster and no fleet.
#
# `make frontend-<zone>` above starts the zone alone, which leaves its /api pointed at a backend
# nobody started (home/lakehouse default to :8001, the lineage service — which `make dev-micro` does
# not run, so every call fails on a connection refused). This starts that zone's own seed-driven
# mocks and points it at them, which is the same machinery its Playwright suite uses — so it works
# with no k3s, no uvicorn fleet, and no container runtime. That is what makes it the loop for a cloud
# sandbox (claude.ai/code, CI) as well as for a laptop.
#
# Mocks answer 404 until seeded, auth is OFF, and cross-zone links 404 because only one zone listens.
# `compute` and `studio` have no mocks at all — the script says so instead of pretending. Details and
# the full trap list: .claude/skills/rask-frontend (§ Develop ONE zone, no cluster).
dev-zone:             # one zone + fake upstreams, e.g. `make dev-zone ZONE=lakehouse`
	@test -n "$(ZONE)" || { echo "  !! usage: make dev-zone ZONE=lakehouse   (zones: $(ZONES))"; exit 1; }
	bun --cwd=frontend/packages/zone-contract run dev:zone $(ZONE)

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

# ray-up exports S3_SECRET (+ the lineage token) to the LOCAL head because the submission body no
# longer carries them (the Ray Jobs API echoes runtime_env to any reader — docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md
# "The Python estate audit").
# In-cluster the pods hold them via secretKeyRef; locally the head process env is the pod. The
# rustfsadmin default is the dev store's published constant.
ray-up:
	@if ray status >/dev/null 2>&1; then \
	  echo "Ray already running. ray-status / ray-down to inspect / stop."; \
	else \
	  S3_SECRET=$${S3_SECRET:-rustfsadmin} LINEAGE_SERVICE_TOKEN=$${APP_API_TOKEN:-} \
	  S3_ENDPOINT=$${S3_ENDPOINT:-http://localhost:9000} S3_REGION=$${S3_REGION:-us-east-1} \
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
# The media plane (viewer and search) is a client the product chart does not deploy; with its release running and
# empty, /explorer finds nothing, /annotator has no page and /lakehouse lists no tables — and "not seeded" is
# indistinguishable from "not built". One command fixes that for the whole estate.
seed-dev: ## Seed the dev estate: media corpus + a labeling task with items (needs a running cluster)
	scripts/seed_dev_estate.sh

# ---- rustfs (S3-compatible object storage) smoke ---------------------------
# Prove packages/storage + LanceDB work against a REAL rustfs backend (not moto).
# rustfs serves the S3 API on :9000; rask is storage-agnostic, so this is env-only.
# ONE command, not three. This replaced `rustfs-up` (a DETACHED `docker run`), `smoke-rustfs` and
# `rustfs-down` — the last of which an operator had to remember. The service is bound for exactly the
# life of the smoke: nothing is detached, nothing leaks when it fails, and no host port is taken, so it
# cannot collide with a `make dev-*` stack or a second developer on the same box.
#
# The old shape's `docker run` was one of the estate's last three, and the stated objection to
# converting it was that `as-service up` holds a terminal where `up -d` does not. That is true of a
# two-step shape and irrelevant to a one-step one.
smoke-rustfs: ## Storage smoke vs a REAL rustfs S3 backend (round-trip + LanceDB over s3://)
	dagger call smoke-rustfs

# The storage-AGNOSTIC proof: the catalog lifecycle e2e with the bytes on a RustFS service bound to one
# exec. Replaced `scripts/rustfs_e2e.sh`, which drove `docker compose up -d --build` over two
# compose files and was invisible to the estate's own docker gate for most of a day — the gate wanted
# `up` on the same line as `compose`, and the script wrapped it.
rustfs-lifecycle: ## Catalog lifecycle e2e with the bytes on RustFS (proves S3-agnosticism)
	dagger call rustfs-lifecycle

# The scoped-users hook's provisioning script against a throwaway RustFS that boots as the fixture does (root pair
# from files): a policy the store refuses fails the run before it binds anyone (XC-163).
rustfs-provisioning: ## Scoped-users provisioning vs a REAL RustFS (a refused policy fails the hook, binds nobody)
	dagger call rustfs-provisioning

# The app-side-seeding authorization chain against a REAL Dex and a REAL OpenFGA: seven steps proving
# the app seeds ownership tuples on create (no tuple is written by hand), that owner cascades to
# reader and writer, and that a second identity holding no grant gets 403 on both.
#
# `scripts/auth_e2e.sh` is GONE. It existed only to bring up a compose stack for the CI job, and that
# job now calls this target's function directly; the assertions live on in `scripts/auth_chain.sh`.
auth-chain: ## Dex + OpenFGA authorization chain e2e (app-side tuple seeding)
	dagger call auth-chain

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
	dagger call governance-chain

# The NATS permission table (`nats.auth.users`) on a real broker: the chart's nats-server in operator mode
# with test-only keys, every bus client driven as its own user and refused on every subject it does not
# own, and real daprd running the chart's Components against it ([[XC-078]]). `dagger call nats-auth
# --values=<file>` reads the table from another values file, e.g. a copy with one allowance deleted.
nats-auth: ## NATS auth e2e — each bus client admitted only to its own subjects (operator-mode broker + daprd)
	dagger call nats-auth

# ---- local k3s ------------------------------------------------------------
# A service that is in neither this list nor $(K3S_IMAGES) is never built by `make k3s-build` and never
# side-loaded by `make k3s-import`, so a hand-built image lands outside CONTAINERD and k3s answers
# ErrImagePull. Measured 2026-08-06: a Dagger build succeeded (exit 0), `kubectl set image` ->
# ErrImagePull, rolled back.
# `notifications` joins for exactly that reason, in the same commit as its dockerfile and its chart
# Deployment — the three are one act, and any two of them without the third is the ErrImagePull above.
# `annotator` is a client with its own release (deploy/fixtures/annotator, ADR 0171) and its own image, and so is
# `table-maintenance`, the bring-your-own compaction job (deploy/fixtures/table-maintenance, [[LH-529]]).
# `media` is the viewer and search, clients of the lakehouse that build apart from its image ([[XC-177]]); the product
# chart's explorer runs it.
COMPOSE_IMAGES = compute notifications flows annotator table-maintenance media
KUBECONFIG ?= /etc/rancher/k3s/k3s.yaml
# Routed through scripts/helm.sh, which sets HELM_DRIVER=sql. NOT bare `helm`: the release lives in
# Postgres, and against the default Secret backend helm reports it ABSENT and `upgrade --install`
# re-installs over a live estate WITHOUT erroring. Read-only subcommands pass straight through.
# The release's namespace, stated rather than inherited from the kubeconfig context ([[XC-152]]): the fixtures it reads
# by name (rask-rustfs, rask-dex, rask-openbao, rask-nats, compute) install into `default` (each fixture.env), so the
# release must share it. Helm reads HELM_NAMESPACE; kubectl takes the flag, and a later `-n`/`-A` in a recipe wins.
RELEASE_NAMESPACE ?= default
HELM ?= KUBECONFIG=$(KUBECONFIG) HELM_NAMESPACE=$(RELEASE_NAMESPACE) ./scripts/helm.sh
KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl --namespace $(RELEASE_NAMESPACE)
# lance-rest-catalog is the ONE lakehouse image (catalog + lineage + maintenance — chart `image.catalog`);
# the default render runs its containers from it, so the
# build/import set must carry it or kind/k3s deploys ImagePullBackOff on every lakehouse pod.
# The object store fixture runs the upstream RustFS image by digest, which the node pulls; nothing here builds it.
K3S_IMAGES = $(COMPOSE_IMAGES) ray-cluster lance-rest-catalog
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

k3s-build: ## Build all fleet + ray-cluster images as OCI tarballs in K3S_IMAGE_DIR (via Dagger, no docker)
	@for s in $(COMPOSE_IMAGES); do \
	  bash scripts/dagger-image.sh --name $$s --out $(K3S_IMAGE_DIR)/$$s.tar || exit 1; \
	done
	bash scripts/dagger-image.sh --name ray-cluster --out $(K3S_IMAGE_DIR)/ray-cluster.tar
	# The lakehouse fleet image — dockerfile name (rest-catalog) != image name, so it can't
	# ride the COMPOSE_IMAGES loop. Same build scripts/e2e_stack.sh does.
	bash scripts/dagger-image.sh --name rest-catalog --out $(K3S_IMAGE_DIR)/lance-rest-catalog.tar

k3s-import: ## Side-load the k3s-build tarballs into k3s containerd as <image>:dev (sudo, no docker)
	@for s in $(K3S_IMAGES); do \
	  echo ">> importing $$s:dev"; \
	  sudo k3s ctr -n k8s.io images import --digests --index-name docker.io/library/$$s:dev $(K3S_IMAGE_DIR)/$$s.tar || exit 1; \
	done

# The LOCAL dev identity values. auth.enabled is ON in the chart's own defaults (a security default must
# fail closed), and the chart REFUSES to render it without an issuer, so the local loop supplies the dev
# Dex's here; DEV_ORIGIN is the browser origin the dex fixture registers a sign-in callback for. Neither is
# a secret. Override them from the environment for a differently-addressed dev cluster.
DEV_ISSUER          ?= http://localhost:8080/dex
DEV_ORIGIN          ?= http://localhost:8080
# The store address the catalog vends (LH-489): the rustfs fixture's node port on the node's own address, which a pod
# and this host both reach, where `rask-rustfs:9000` resolves only in the cluster.
DEV_NODE_IP         ?= $(shell $(KUBECTL) get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)
DEV_STORE_NODE_PORT := $(shell sed -n 's/^nodePort: *//p' deploy/fixtures/rustfs/chart/values.yaml)
DEV_VENDED_ENDPOINT ?= http://$(DEV_NODE_IP):$(DEV_STORE_NODE_PORT)

# THE CREDENTIAL KEYS A RELEASE MAY STILL CARRY from before [[XC-004]], dropped from every upgrade that
# re-feeds the live values (`-f $$LIVE`). templates/prod-credentials.yaml refuses each of them as a value,
# and `null` is how Helm removes a key a values file supplies. The two lists name the same keys.
DROP_CREDENTIAL_VALUES := --set dapr.appToken=null \
  --set secrets.hfToken=null --set ray.auth.token=null --set age.password=null --set objectStore.secretKey=null \
  --set objectStore.maintenanceSecretKey=null \
  --set objectStore.catalogSecretKey=null --set objectStore.lineageSecretKey=null

# THE ONE BOOTSTRAP SECRET a dev estate is given by hand: the operator-supplied third-party credentials
# (today the Hugging Face token), which nothing in the cluster can generate. The dev OpenBao seeds the
# store from it (values.yaml `secrets.hfTokenInStore`); the token reaches kubectl on stdin, never argv.

# The prerequisites a dev estate gets from deploy/fixtures, each its own release, installed before rask's
# (NOT FOR PRODUCTION; deploy/fixtures/README.md). Dapr's control plane comes first after ESO, so its injector is
# Ready before any pod that wants a sidecar exists; the NATS bus every pub/sub component dials follows. The CloudNativePG
# operator serves the AGE-on-CNPG Cluster; its CRDs come from k3s-crds, which runs first. Dex is the dev OIDC issuer,
# given the issuer and origin rask is given; rustfs and openbao are the dev object and secret stores, which start once
# rask is installed: the object store's root credential and the secret store's seed scripts are rask's; observability is
# the store, dashboards and alerting rask's Collector exports to; marquez is the graph lineage forwards to and mailpit the
# relay the email channel sends to. A GPU node with no device plugin yet adds nvidia-device-plugin.
FIXTURES ?= external-secrets dapr nats cloudnative-pg dex rustfs openbao observability marquez mailpit

# THE CLIENTS, each its own release and none a prerequisite: `k3s-up` installs the lakehouse alone, and `k3s-clients`
# adds them after it (XC-183), each reaching the lakehouse by reference. The Ray engine is two releases, the operator
# first, and compute (the Ray dashboard and Serve companion) follows it, reading rask's app token and Dapr Configuration;
# table-maintenance is the bring-your-own compaction job ([[LH-529]]); the annotator keeps its state on the
# cloudnative-pg fixture (ADR 0171).
CLIENTS ?= kuberay-operator ray-cluster compute table-maintenance annotator

k3s-fixtures: ## Install the development fixtures (FIXTURES) as their own releases, before the rask chart
	$(if $(filter-out dex,$(FIXTURES)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh $(filter-out dex,$(FIXTURES)))
	$(if $(filter dex,$(FIXTURES)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh dex -- --set-string issuer=$(DEV_ISSUER) --set-string publicOrigin=$(DEV_ORIGIN))

k3s-clients: ## Install the lakehouse's clients (CLIENTS) as their own releases, after `k3s-up`: each reaches it by reference
	$(if $(filter-out compute annotator,$(CLIENTS)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh $(filter-out compute annotator,$(CLIENTS)))
	$(if $(filter compute,$(CLIENTS)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh compute -- --set-string auth.oidc.issuer=$(DEV_ISSUER))
	$(if $(filter annotator,$(CLIENTS)),KUBECONFIG=$(KUBECONFIG) ./scripts/fixtures.sh annotator -- --set-string auth.oidc.issuer=$(DEV_ISSUER))

k3s-crds: ## Apply the vendored CRDs no release owns (see chart/.helmignore), before the cloudnative-pg fixture
	@# Server-side apply: the CNPG CRD set exceeds kubectl's client-side annotation limit.
	kubectl apply --server-side -f chart/crds-bootstrap/

# `-o yaml` on the `helm get values` below is LOAD-BEARING, not a style choice. Bare `helm get values`
# prints a HUMAN header line ("USER-SUPPLIED VALUES:") above the YAML; feeding that straight back
# through `-f` parses the header as a KEY, so every upgrade re-stored a junk `USER-SUPPLIED VALUES:
# null` entry in the release and carried it forward forever (observed live 2026-08-16 on rev 6).
#
# This note lives ABOVE the target rather than beside the line it describes, and that is deliberate:
# the recipe is ONE shell command joined by trailing backslashes, so a `@#` line placed inside the
# continuation is not a comment at all — make hands it to sh as a command. It did, from 2026-08-16
# (7c2171f0) until 2026-08-23, and the whole target failed with `/bin/sh: @#: not found` plus a
# `helm get values requires 1 argument` from the orphaned `-o yaml`. A comment that breaks the recipe
# it documents is worse than no comment; keep prose out of the continuation.
k3s-up: k3s-crds k3s-fixtures k3s-deps k3s-stem-check ## Apply CRDs, install fixtures, vendor deps, then install/upgrade the release and wait for the catalog
	@case '$(DEV_VENDED_ENDPOINT)' in http://:*|*:) echo "DEV_VENDED_ENDPOINT '$(DEV_VENDED_ENDPOINT)' names no node address or port: is kubectl pointed at the cluster?" >&2; exit 1;; esac
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ -z "$$HF_TOKEN" ] && [ -r "$${HF_HOME:-$$HOME/.cache/huggingface}/token" ]; then \
	  HF_TOKEN="$$(cat "$${HF_HOME:-$$HOME/.cache/huggingface}/token")"; \
	  echo ">> HF_TOKEN from $${HF_HOME:-$$HOME/.cache/huggingface}/token (hf auth login)"; \
	fi; \
	if [ -z "$$HF_TOKEN" ]; then echo "WARN: no HF token (env, .env, or 'hf auth login') — htrflow Serve will 401 on the gated TrOCR model"; fi; \
	HFARGS=; \
	if [ -n "$$HF_TOKEN" ]; then \
	  printf '%s' "$$HF_TOKEN" | $(KUBECTL) create secret generic rask-supplied-credentials --from-file=hf-token=/dev/stdin --dry-run=client -o yaml | $(KUBECTL) apply -f - >/dev/null || exit 1; \
	  HFARGS="--set secrets.hfTokenInStore=true"; \
	fi; \
	: # THE PINS FILE CARRIES TAGS AND NOTHING ELSE, so it LAYERS over the registry rather than replacing
	: # it. `k3s-pins.sh` strips the registry on purpose ("the chart supplies it from image.repository"),
	: # so selecting the pins file INSTEAD of the live values left `image.repository` unset and the chart
	: # refused the render outright — "must be set to a registry ... or set image.localImages=true".
	: # Measured 2026-09-09: every `make k3s-up` failed for as long as a non-empty pins file existed,
	: # which is the state a pin is FOR. Base first (the live release's image settings, else side-loaded),
	: # pins second, so the tags win and the registry survives.
	IMGARGS="--set image.localImages=true"; \
	LIVE=$$(mktemp); \
	if $(HELM) get values rask -o yaml >"$$LIVE" 2>/dev/null && grep -q 'repository:' "$$LIVE"; then \
	  IMGARGS="-f $$LIVE"; \
	  echo ">> reusing the LIVE release's image settings ($$(grep -c . "$$LIVE") values) — not imposing localImages"; \
	fi; \
	if [ -s deploy/values-live-pins.yaml ]; then \
	  IMGARGS="$$IMGARGS -f deploy/values-live-pins.yaml"; \
	  echo ">> image TAGS pinned from deploy/values-live-pins.yaml (make k3s-pins to refresh)"; \
	fi; \
	$(HELM) upgrade --install rask ./chart --wait --wait-for-jobs --timeout 20m \
	  --take-ownership \
	  $$IMGARGS \
	  -f deploy/values-local.yaml \
	  --set-string auth.oidc.issuer=$(DEV_ISSUER) \
	  --set-string vending.endpoint=$(DEV_VENDED_ENDPOINT) \
	  $(DROP_CREDENTIAL_VALUES) \
	  $$HFARGS \
	  $${AWS_ACCESS_KEY_ID:+--set-string objectStore.accessKey=$$AWS_ACCESS_KEY_ID}
	$(KUBECTL) rollout status deploy/rask-catalog --timeout=300s
	@echo "API → http://<node-ip>/api/catalog/livez   (host-less routes on Traefik's Gateway; over VS Code/ssh -L forward port 80 → http://localhost:<port>/)"

seed-corpus: ## Seed the demo corpus into the volume the media plane actually READS
	./scripts/seed-corpus.sh

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

k3s-pins: ## Capture what the cluster is RUNNING into deploy/values-live-pins.yaml (#135)
	@# KUBECTL is deliberately NOT passed through. `KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl` is a
	@# multi-word string carrying its own env assignment, so `KUBECTL=$(KUBECTL) ./script` made the shell
	@# read `KUBECTL=KUBECONFIG=...` as an assignment and then run `kubectl ./scripts/k3s-pins.sh` —
	@# `error: unknown command "./scripts/k3s-pins.sh" for "kubectl"`. The script quotes `"$$KUBECTL"`,
	@# so passing it correctly would fail too: it would exec a binary literally named
	@# `KUBECONFIG=... kubectl`. KUBECONFIG is already in the environment; the script's own `kubectl`
	@# default is the right one.
	@KUBECONFIG=$(KUBECONFIG) RELEASE_NAMESPACE=$(RELEASE_NAMESPACE) ./scripts/k3s-pins.sh deploy/values-live-pins.yaml
	@echo ">> now: make k3s-up   (or ./scripts/helm.sh upgrade rask ./chart -f deploy/values-live-pins.yaml)"
	@echo ">> NOT bare 'helm upgrade' — every helm call goes through scripts/helm.sh"

k3s-converge: ## Roll a WHOLE image stem to one tag and upgrade (the resolution k3s-stem-check names). STEM=lance-rest-catalog TAG=main-<sha>
	@# THE DOCUMENTED FIX FOR A SPLIT STEM, made runnable. `k3s-stem-check` refuses `k3s-up` while one
	@# stem runs several tags and tells the operator to "rebuild one image from a commit carrying every
	@# change, roll the whole stem, re-run" — but the rolling half had no command, so it was done by
	@# hand with `kubectl set image` per deployment. That is the SECOND WRITER the chart's own comments
	@# warn about: a partial roll leaves the release pinning the other half, and the next `helm upgrade`
	@# silently reverts it.
	@#
	@# One helm upgrade instead, which converges every workload on the stem in a single transaction and
	@# leaves the release agreeing with the cluster. It deliberately does NOT depend on k3s-stem-check:
	@# this target is what RESOLVES the split the check refuses to proceed through.
	@#
	@# Image settings come from the LIVE release, exactly as k3s-up derives them, so a stem this call
	@# does not name keeps the tag it is running rather than falling back to the chart default.
	@# THE UPGRADE'S STATUS, NOT THE CLEANUP'S. A shell reports its LAST command's exit code, so
	@# `helm upgrade ...; rm -f "$$LIVE"` hands make the `rm`'s status and a FAILED upgrade reports
	@# success. Measured here: a post-upgrade hook timed out, helm printed `UPGRADE FAILED`, and the
	@# target still exited 0 and printed ">> every stem converged" — a half-applied deploy reading as
	@# clean, which is the worst direction for this failure to go. So the recipe keeps the upgrade's
	@# status in rc and exits with it after the cleanup.
	@test -n "$(TAG)" || { echo "!! TAG is required, e.g. make k3s-converge TAG=main-$$(git rev-parse --short=8 HEAD)"; exit 2; }
	@set -a; [ -f .env ] && . ./.env; set +a; 	STEM="$${STEM:-lance-rest-catalog}"; 	LIVE=$$(mktemp); 	$(HELM) get values rask -o yaml >"$$LIVE" 2>/dev/null || { echo "!! no live release to read image settings from"; exit 1; }; 	grep -q 'repository:' "$$LIVE" || { echo "!! the live release carries no image.repository; run make k3s-up first"; exit 1; }; 	echo ">> converging stem $$STEM -> $(TAG)"; 	$(HELM) upgrade --install rask ./chart --wait --wait-for-jobs --timeout 20m --take-ownership 	  -f "$$LIVE" 	  -f deploy/values-local.yaml 	  --set image.tags.$$STEM=$(TAG) 	  --set-string auth.oidc.issuer=$(DEV_ISSUER) 	  $(DROP_CREDENTIAL_VALUES); 	rc=$$?; 	rm -f "$$LIVE"; 	[ $$rc -eq 0 ] || { echo "!! helm upgrade FAILED (exit $$rc) — the release was NOT converged, and the cluster may now disagree with it"; exit $$rc; }
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
# kind cluster, which is what the CI live-proof job (e2e-stack) boots. Toolchain
# is pinned into .localbin by `make bootstrap` (kind/kubectl/fga; helm + docker from PATH).
.PHONY: bootstrap kind-up kind-images kind-load kind-deploy kind-down e2e-ci e2e-isolation e2e-container-deletes e2e-fga-model

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

kind-images: ## Build the full :dev image set into docker's image store, which `kind load docker-image` reads (same builds k3s uses)
	@for s in $(COMPOSE_IMAGES); do bash scripts/dagger-image.sh --name $$s --tag $$s:dev || exit 1; done
	bash scripts/dagger-image.sh --name ray-cluster --tag ray-cluster:dev
	bash scripts/dagger-image.sh --name rest-catalog --tag lance-rest-catalog:dev

kind-load: ## Side-load the :dev image set into the kind cluster
	$(KIND) load docker-image $(foreach i,$(KIND_IMAGES),$(i):dev) --name $(KIND_CLUSTER)

# Third-party images the chart pulls (openfga, dex, openbao, otel-collector, …). kind's containerd is a SEPARATE image store from the
# host's docker, so a fresh cluster re-pulls every one of them from the internet — ~20 minutes of
# pure network before a single app pod starts. The host docker cache, by contrast, persists across
# clusters: pull once here, side-load, and every later cluster starts in seconds. Derived from the
# actual render (not a hand-list, which would rot) with our own :dev images filtered out. An image
# with NO tag is skipped (a subchart default the render leaves incomplete — the pod that resolves it
# pulls it). The character classes are DELIBERATELY case-sensitive-inclusive: docker tags allow
# uppercase, and a lowercase-only class silently TRUNCATES `apache/age:release_PG16_1.5.0` to
# `apache/age:release_`, so the image the OpenFGA migration blocks on would be the one this target
# never preloads.
kind-preload: bootstrap ## Pull the chart's third-party images into the host cache, then side-load them
	@imgs=$$(helm template rask ./chart --set singleTenant.enabled=true \
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
	$(HELM) upgrade --install rask ./chart --kube-context kind-$(KIND_CLUSTER) --wait --wait-for-jobs --timeout 900s
	$(LOCALBIN)/kubectl --context kind-$(KIND_CLUSTER) rollout status deploy/rask-catalog --timeout=300s

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
# picks one: bob is in `team:eng`, bound to `project:acme`, so he holds `can_administer` and would make
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
E2E_SUITES = annotator byo-branch byo-erasure byo-pipeline cas chaos compaction edge media-catalog observability phase1 soak track-a
.PHONY: $(addprefix e2e-,$(E2E_SUITES))   # declared HERE, not up with the other .PHONY: make
                                          # expands a rule's prerequisites AS IT READS the line,
                                          # so referencing E2E_SUITES before this assignment
                                          # expands to nothing and silently declares no target.

e2e-annotator:      ## XC-167 live proof: the annotator, from its own release, works a scratch tenant through the lakehouse's public doors as the person and publishes as its own account (`bash scripts/e2e_live.sh tests/e2e-py services/annotator/tests/live -m annotator --require-live` discovers its addresses)
	@test -n "$(LANCE_E2E_ANNOTATOR_URL)" || { echo "  !! e2e-annotator needs LANCE_E2E_ANNOTATOR_URL, LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX, LANCE_E2E_NOTIFICATIONS_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	uv run pytest tests/e2e-py services/annotator/tests/live -m annotator --require-live -v
e2e-byo-branch:     ## LH-490 live proof: a branch the owner creates through CreateTableBranch takes a writer's stock append, delete and update through version/create on the table's vended credential, main is unchanged, a refused branch commit answers a typed error, and the store refuses the credential a final manifest and a ref (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_a_stock_client_commits_on_a_branch.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-branch needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-branch needs LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_branch -v --require-live
e2e-byo-erasure:    ## LH-493 live proof: the erasure door records an obligation and commits nothing, a writer+maintainer job rewrites through version/create, a skipped compaction stays named, and the catalog releases what it verified (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_an_erasure_is_rewritten_by_a_bring_your_own_job.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-erasure needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_erasure -v --require-live
e2e-byo-pipeline:   ## LH-478 live proof: a stock-client pipeline seeds bronze, derives silver with provenance, is held behind `published`, approved and followed on /events?since= (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX; `bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NOTIFICATIONS_URL)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-byo-pipeline needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_NOTIFICATIONS_URL=$(LANCE_E2E_NOTIFICATIONS_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) uv run pytest tests/e2e-py -m byo_pipeline -v --require-live
e2e-cas:            ## Object-store + registry conditional-write (CAS) proofs (needs LANCE_E2E_S3_ENDPOINT)
	@test -n "$(LANCE_E2E_S3_ENDPOINT)" || { echo "  !! e2e-cas needs LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_S3_ENDPOINT=$(LANCE_E2E_S3_ENDPOINT) uv run pytest tests/e2e-py -m cas -v
e2e-chaos:          ## Criterion 5's dependency-outage drills (XC-095): each takes one dependency of the release down, drives the doors while it is gone, restores it and holds the estate to criteria 1 and 4; they TAKE THE RELEASE'S DEPENDENCIES DOWN, so they run only with --chaos (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on its cluster; `bash scripts/e2e_live.sh -m phase1 --require-live --chaos` discovers them)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NOTIFICATIONS_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3)" || { echo "  !! e2e-chaos needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3, and kubectl on the release's cluster — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_NOTIFICATIONS_URL=$(LANCE_E2E_NOTIFICATIONS_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_S3=$(LANCE_E2E_S3) uv run pytest tests/e2e-py -m chaos -v --require-live --chaos
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
e2e-media-catalog:  ## Annotator/catalog live-mode proof (needs MEDIA_CATALOG_URL)
	@test -n "$(MEDIA_CATALOG_URL)" || { echo "  !! e2e-media-catalog needs MEDIA_CATALOG_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	MEDIA_CATALOG_URL=$(MEDIA_CATALOG_URL) uv run pytest tests/e2e-py services/annotator/tests/live -m media_catalog -v
e2e-observability:  ## OTLP → GreptimeDB observability proof (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_GREPTIME_URL)" || { echo "  !! e2e-observability needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_GREPTIME_URL — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_GREPTIME_URL=$(LANCE_E2E_GREPTIME_URL) uv run pytest tests/e2e-py -m observability -v
e2e-phase1:         ## Phase 1's acceptance (XC-090): one drive of the bring-your-own scenario, its own claims, and each criterion module read off it; criterion 1, provenance, is XC-091, criterion 2, the catalog, is XC-092, criterion 4, events, is XC-094, and criterion 5's drills (XC-095) run only with --chaos (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE; `bash scripts/e2e_live.sh -m phase1 --require-live` discovers them, and with `--chaos --keep-drive PATH` runs the drills and keeps the drive for `make e2e-soak`)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NOTIFICATIONS_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_SERVICE_TOKEN_FILE)" || { echo "  !! e2e-phase1 needs LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_SERVICE_TOKEN_FILE — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_NOTIFICATIONS_URL=$(LANCE_E2E_NOTIFICATIONS_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_SERVICE_TOKEN_FILE=$(LANCE_E2E_SERVICE_TOKEN_FILE) uv run pytest tests/e2e-py -m phase1 -v --require-live
e2e-soak:           ## Phase 1's soak (XC-090): a day after an acceptance run kept its drive (`--keep-drive PATH`), the estate still reads it as the criteria did, nothing parked and both outboxes drained; removes the kept tenant once it passes (needs SOAK, the kept drive's PATH, and LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 with its key pair; `bash scripts/e2e_live.sh -m soak --require-live --soak PATH` discovers them)
	@test -n "$(SOAK)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_LINEAGE_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NOTIFICATIONS_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_DEX)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_NATS_URL)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3)" || { echo "  !! e2e-soak needs SOAK, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL, LANCE_E2E_DEX, LANCE_E2E_NATS_URL, LANCE_E2E_S3 — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_LINEAGE_URL=$(LANCE_E2E_LINEAGE_URL) LANCE_E2E_NOTIFICATIONS_URL=$(LANCE_E2E_NOTIFICATIONS_URL) LANCE_E2E_DEX=$(LANCE_E2E_DEX) LANCE_E2E_NATS_URL=$(LANCE_E2E_NATS_URL) LANCE_E2E_S3=$(LANCE_E2E_S3) uv run pytest tests/e2e-py -m soak -v --require-live --soak $(SOAK)
e2e-track-a:        ## Track A acceptance: tier contract + gate/publish symmetry + branch isolation (needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT)
	@test -n "$(LANCE_E2E_CATALOG_URL)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_TOKEN)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	@test -n "$(LANCE_E2E_S3_ENDPOINT)" || { echo "  !! e2e-track-a needs LANCE_E2E_CATALOG_URL, LANCE_E2E_TOKEN, LANCE_E2E_S3_ENDPOINT — a live drive with no live target is a failed invocation, not a pass"; exit 1; }
	LANCE_E2E_CATALOG_URL=$(LANCE_E2E_CATALOG_URL) LANCE_E2E_TOKEN=$(LANCE_E2E_TOKEN) LANCE_E2E_S3_ENDPOINT=$(LANCE_E2E_S3_ENDPOINT) uv run pytest tests/e2e-py -m track_a -v

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
