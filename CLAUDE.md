# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Engineering principles (read first)

**This is state-of-the-art work. Do things properly — no shortcuts, no band-aids.**

- **Fix root causes, not symptoms.** Never paper over an app/library bug at an outer
  layer (proxy, ingress, env hack, wrapper) when the defect belongs in the app or its
  build. Workarounds that "make it pass" are not acceptable as final fixes — at most a
  clearly-labelled temporary step that is then replaced by the real fix.
- **A fix must travel with the code.** Prefer fixes that keep a component correct on its
  own (behind any proxy, in any environment), over fixes that depend on surrounding infra.
- **Verify like it ships.** SSR returning 200 is not "working" — exercise the real user
  path (a browser for UI, the actual client for APIs). Assume nothing is fixed until it's
  been observed working end-to-end.
- **Plan before editing, and test first.** A non-trivial change gets a plan you can state, a RED test
  that proves the defect, then the fix — not ad-hoc edits.
- **Tests: invoke the testing skills first, every time.** Before writing, changing, deleting or auditing any test,
  invoke `testing-python` and read `writing-python`'s `references/testing.md` (the principles, which hold for any
  codebase; `fastapi`'s testing reference for a FastAPI door), then `rask-testing` (only rask's pytest wiring,
  fixtures and the row rule). Integration tests (the real app over HTTP, real pylance on moto, a real OpenFGA, real uvicorn,
  several services in one flow) are the keepers; a unit test earns its place only for a pure function's distinct
  branches, and a unit test covering what an integration test covers is deleted, not kept. A change adds at most
  one test per closes-when clause and replaces the test of the same seam rather than adding beside it; state the
  net test delta in the commit. No test reads source text, an AST, prose or file names: a code-shape rule belongs in
  ruff or an import-linter contract, or nowhere. Measured 2026-09-28: 12,066 tests in 1,291 files (231k test lines
  against 120k production lines), and 2,743 of them (22.7%) execute no production line at all.
- **No silent scope-cuts.** If you bound coverage, sample, or defer something, say so
  explicitly. Don't let "partially done" read as "done".
- **No backward compatibility — the estate is pre-production.** No migration shims, dual read paths,
  deprecation windows or "keep the old key working": change the thing and move every consumer in the
  same commit. Current state is test data.
- **Lance-idiomatic, answered from `lance_docs/`, never guessed.** A question about Lance or
  lance-namespace behaviour is settled by citing `lance_docs/` (`ns_catalog/spec.yaml` is authoritative;
  `namespace.md` is an older bundle that contradicts it in places) or by measuring on the installed
  pylance — never from memory.
- **Lakekeeper is the reference, not the stack.** For how zero trust, authn/authz, governance,
  provenance, events and secrets are solved well, read Lakekeeper (`github.com/lakekeeper/lakekeeper`
  and `lakekeeper-charts`). rask translates those answers onto ITS stack — Lance + lance-namespace, Dapr,
  NATS JetStream, OpenFGA, Dex — and imports neither Iceberg nor Lakekeeper's own stack choices.
- **Python follows the ra-skills `writing-python` and `fastapi` references, read before writing:**
  Pydantic-only structured models, typed `lance_namespace` errors from catalog code, `TypeError` for a
  wrong type, no `# type: ignore` / `# ty: ignore` (narrow or cast).
- **Finish Phase 1 first; the backlog does not grow without the owner.** The work is the GitHub Issues on
  `AI-Riksarkivet/rask` (one issue per row, title `LH-243: …`), in the FOCUS order of the pinned map issue
  #478 "Phase 1 map" (`docs/agents/issue-tracker.md`). The **production short list** comes first (owner, 2026-10-02): the
  issues labelled `blocks-prod`, with CP-029 and LH-226 (criterion 3) and their enablers; every other Phase 1 row waits
  under the `phase-1-after-launch` label. Phase 1 (the lakehouse) is done when its five criteria hold
  together on the estate: (1) provenance/lineage correct, (2) catalog correct for lance-ns and
  authz/governance, (3) not coupled to a workflow engine or Ray, (4) events correct, (5) resilient
  (XC-090 is the proof). Never add a counted issue without asking the owner; a found problem becomes an issue
  labelled `parked` only with the owner's say, and stays out of every count. A row closes when its fix is deployed
  and read back live, not when it merges.
  **Phase 1 IS the lakehouse's components, and nothing else comes first** (owner, 2026-09-28, said more than
  once; widened 2026-09-29): **medallion** (bronze→silver→gold), **catalog**, **provenance/lineage**,
  **FGA/governance**, **maintenance**, and the
  **notifications** service. Compute comes second. Search, the viewer (explorer), the annotator and flows are
  ignored for now; the models zone and Kueue are decoupled and LOW. A cross-cutting or infra row (chart, CI,
  Kueue) is taken only as the stated enabler of a named Phase 1 row.
- **Owner decisions go through the multi-question tool**, each option with a concrete what, how and
  why — never as a question trailing a report.
- **Stopping rules (owner, 2026-10-05).** When a step doesn't need the owner's input, keep going. Put
  status notes in the same message as the next action. Stop and ask only when you cannot continue without
  the owner, or before anything destructive. Every delegated agent's report marks what it could not
  confirm and where it looked; a reviewer lists only problems it would block the merge for, each with
  the file and line, why it is wrong, and how to show it fails.
- **Three facts judge every design (owner, 2026-10-12): zero trust, cloud native, Lance-idiomatic.** Put them in
  every agent brief and test every option against them before offering it. A change whose purpose is closing a
  zero-trust gap (an unsigned writer admitted, a credential that never rotates, an identity trusted by position) is
  pre-approved: do it and report it. Still ask before anything destructive on shared live state, and still ask a
  real trade-off between two zero-trust-correct designs.
- **Platform services are external in production** (owner, 2026-10-04). OpenBao, Dex, S3, the OTel backend, ESO, the
  operators, NATS and OpenFGA's Postgres are the platform's; the bundled copies are development fixtures. rask
  references them by name, mints no production trust root, and does not engineer a fixture's durability. The app
  chart is the product (ADR 0153): operators, CRDs and compute go in their own releases.
- **Research before asking** (owner, 2026-09-26). A question `lance_docs/`, a measurement or rask's own callers can
  answer is researched and decided, with the evidence reported; only product scope, risk appetite, priority, facts
  outside the repo and outward-facing actions go to the owner. Justify a design from `lance_docs/`, never from what
  Lakekeeper chose (owner, 2026-10-10). When the owner asks how or whether, answer in plain prose with one
  recommendation and the next command, not a menu. `/implement`, `/to-spec`, `/to-tickets`, `/grill-with-docs` and
  `/wayfinder` are started by the owner only.
- **Delegation** (owner, 2026-10-05): implementer, reviewer and workflow agents run Opus at medium effort
  (`subagent_type: opus-medium`); read-only sweeps may use Sonnet. A review round fixes HIGH and MEDIUM findings only,
  LOWs are parked, and one round is the default. A worktree agent starts from `origin/main`, so pin its base with
  `git switch -c <branch> <sha>` when local `main` is ahead.
- **Live estate** (owner, 2026-10-02/2026-09-26). An unplanned write to a shared live object (patch, delete, scale) is
  asked first through the multi-question tool, naming the object, the change and the effect, even under a standing
  kubectl permission. Squash a row's review rounds into one commit before building its images (tags name a sha), and
  deploy the chart from the worktree at the image's sha. The owner's measure is closed rows and shipped code: surface
  a blocking decision early instead of auditing around it.
- **Lessons the estate taught** (traps that recur, each measured once) are listed in `docs/agents/lessons.md`. Read
  it before live work on the estate.

## Toolchain rules

- **NEVER EVER USE DOCKER. ALWAYS USE DAGGER, WHEREVER DAGGER CAN DO IT. This is not negotiable.**
  Not `docker build`, not `docker buildx`, not `docker run`, not `docker compose` — no docker
  command, for any purpose, including throwaway containers you intend to delete. If you are about to
  reach for docker, the answer is a Dagger equivalent; if you think you have found an exception, you
  have not.
  **DAGGER BUILDS EVERY IMAGE. NOT DOCKER.** Local and CI both reach
  BuildKit through `dagger call` against `.dagger/images.go` — `dagger call image --name=<stem>` for
  anything in `.docker/`, `dagger call zone-image --zone=<zone>` for a micro-frontend. **`docker build`
  and `docker buildx build` must not appear** in the `Makefile`, `scripts/` or `.github/workflows/`;
  `scripts/dagger-image.sh` is the single seam every build goes through.
  The dockerfile stays the single source of truth — Dagger changes the *driver*, not the definition, so
  a build cannot behave one way locally and another in CI.
  **Do not add a docker fallback switch.** An escape hatch was added once and rejected outright; it is
  gone. "Leave it on docker for now" is not an available answer — if a tool cannot work with a
  Dagger-built image, solve it or drop the tool.
  **NEVER DOCKER, FULL STOP — the rule is not limited to builds.** This bullet used to name only
  `docker build`/`buildx`, and that scoping was read (2026-08-15) as licence to `docker run` a
  throwaway NATS for a test repro. It is not. **Any** container — ephemeral brokers, one-off
  fixtures, ad-hoc debugging — goes through Dagger, and there is no "it is only temporary" or "I am
  not building anything" exemption. No module is needed for an ad-hoc service:
  `dagger core container from --address=<img> with-exposed-port --port=<p> with-default-args
  --args=<cmd> as-service up --ports=<host>:<p>`.
- **JS/TS uses Bun exclusively.** Use `bun` / `bunx`. `npm`, `npx`, `pnpm`, `pnpx` are not on PATH and MCP install commands assume `bunx`.
- **The JS/TS plane lives in `frontend/`** — its own bun + Turborepo workspace root (its own `package.json`, `bun.lock`, `turbo.json`). Every bun/turbo call is **scoped to it**: `bun --cwd=frontend run <task>`, `bunx turbo --cwd=frontend run <task>`. Use the `--cwd=` form — `bun --cwd <path>` with a space silently no-ops.
- **JS/TS lint + format is oxlint + oxfmt**, not ESLint/Prettier (both deleted). Svelte support comes from `@rsvelte/oxlint-plugin` (lint) and `@rsvelte/fmt` (format); configs live at `frontend/.oxlintrc.json` and `frontend/.oxfmtrc.json`. `lint` / `fmt` / `fmt:check` are **per-package turbo tasks**, run from `frontend/`.
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uv run ty check` (the locked ty; `uvx ty` resolves the newest release and drifts from the lock).
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal                         | Command                                                                  |
| ---------------------------- | ------------------------------------------------------------------------ |
| First-time setup             | `make install` (= `bun --cwd=frontend install` + `uv sync --all-packages` + git hooks + `make k3s-deps` (subcharts) + `make claude-bootstrap`, which installs the ra-skills and mattpocock plugins) |
| Build everything             | `make build`                                                             |
| Run tests (excludes slow)    | `make test` (= `uv run pytest -m "not slow"`)                            |
| Run all tests incl. slow     | `make test-slow` (needs real models / a GPU)                             |
| Single Python test           | `uv run pytest services/catalog/tests/<file>.py::test_name`             |
| Filter by name               | `uv run pytest -k <pattern>`                                             |
| Skip slow tests              | `uv run pytest -m "not slow"`                                            |
| Format + lint + typecheck    | `make check` (= `make fmt` + `make lint` + `make lint-imports` + `make typecheck` + `make knip` + `make fga-test` + `make go-fmt`) |
| Scan dependencies for CVEs   | `make audit` (osv-scanner over all six lockfiles + `.dagger/go.mod`)     |
| Scan dockerfiles + chart     | `make scan-config` (trivy misconfig + secret detection)                  |
| Scan git history for secrets | `make scan-secrets` (trufflehog; gates on VERIFIED credentials only)     |
| Scan a built image           | `make scan-image NAME=gateway` / `make scan-zone-image ZONE=home`        |
| Frontend type-check only     | `bun --cwd=frontend run check` (one zone: `bunx turbo --cwd=frontend run check --filter=home`) |
| Storybook for `@rask/ui`     | `make storybook` (→ `:6006`)                                             |
| Bootstrap Claude Code config | `make claude-bootstrap`                                                  |

### Run the app locally

```bash
make dev-micro         # the fleet: gateway :8888 + compute :8804 + flows :8840 + notifications :8850 +
                       #   the explorer trio from :8101 (via scripts/dev-micro.sh)
make dev-frontends     # ALL 7 zones + the :3024 composition proxy (builds @rask/ui first)
make dev-zone ZONE=lakehouse   # ONE zone + its FAKE upstreams — no cluster, no fleet, no docker
make home              # the catch-all zone alone, :5273
make frontend-explorer # one zone alone (frontend-<zone>: lakehouse|explorer|annotator|compute|studio|models)
```

**`make dev-zone ZONE=<z>` is the leanest loop, and the only one that works with no cluster at all** —
it starts that zone plus the seed-driven mock upstreams its own Playwright suite uses, so it also runs
in a cloud sandbox (claude.ai/code) or CI, where Dagger and k3s cannot. Auth is OFF and cross-zone links
404 because one zone is listening. Populated data needs an `e2e/dev-seed.ts` — **`lakehouse`,
`explorer`, `annotator` and `models` have one**; `home` mocks its upstreams but ships no seed, so it
renders its EMPTY state (the launcher says which). `compute` and `studio` ship no mocks at all (no
`e2e/`, no `test:e2e`) — they start, but unmocked. Full trap list and the coverage
table: `.claude/skills/rask-frontend` § *Develop ONE zone, no cluster*.

**`make frontend-<zone>` does NOT actually isolate a zone**, despite the name: it goes through
`turbo run dev --filter=<zone>...`, which also starts turbo's built-in microfrontends proxy on **:3024**
and `@rask/ui`'s watcher — so it fails outright if anything already holds :3024. Use `dev-zone`.

**Open `:3024`, not a zone's own port** — that is the composition proxy that routes
`/<zone>` to the right dev server. `scripts/dev-micro.sh` is the source of truth for the
fleet's process list and ports.

`make dev-frontends` builds `@rask/ui` + `@rask/api` before starting the zones on purpose:
an unfiltered `turbo run dev` also starts the ui library's `svelte-package -w` watcher,
which rewrites `dist/` while the zones read it — one zone crashes and turbo tears the whole
run down.

### The in-cluster loop: k3s + k9s

`make dev-micro` cannot reproduce anything that only manifests IN-CLUSTER — Dapr sidecar injection,
the catalog's commit announcements reaching lineage's durable feed over the bus, FGA checks. For those, build and deploy:

```bash
make k3s-up          # the cluster + release (one-time per session)
make dev-registry    # ONCE per host: registry on :5000 + point k3s at it (sudo; restarts k3s)
make dagger-engine   # ONCE per host: a Dagger engine that may push to that (plain-HTTP) registry
make k9s             # inspect the cluster (installed into .localbin by `make bootstrap`)
```

Images are built by Dagger and pushed to that registry:
`dagger call image --name=gateway publish --address=172.17.0.1:5000/gateway:<tag>`, then
`kubectl set image` or a chart value. **For pure UI work `make dev-frontends` is the faster loop** —
Vite HMR, sub-second, no cluster involved.

**Tilt was REMOVED 2026-08-04.** It bought an in-cluster hot-reload (~15 s for a zone edit) and cost
a 479-line Tiltfile, four make targets, a `dev.reload` values flag whose only job was to relax
`readOnlyRootFilesystem`, a `VENV_OWNER` build arg in four dockerfiles, and — the reason it went — a
SECOND writer to the cluster. Nobody was using it, and a `helm upgrade` that lands while it is up
silently replaces every image it injected. If an in-cluster backend loop is ever wanted again, the
honest version is one owner, not two.

Two things bite when pushing to the local registry, and neither announces itself:

- **The registry is addressed twice.** k3s pulls via `localhost:5000`; Dagger pushes to
  `172.17.0.1:5000`. Same container — but Dagger's engine *is* a container, so `localhost` inside it
  is the engine. Use the bridge gateway for anything Dagger does.
- **Dagger always speaks HTTPS and `publish` has no `--insecure` flag**, so against the TLS-less dev
  registry it dies with `http: server gave HTTP response to HTTPS client`. The only lever is the
  engine's own BuildKit config, which the auto-provisioned engine cannot receive — hence
  `make dagger-engine`. Re-run it after a Dagger CLI upgrade: a version mismatch makes the CLI
  quietly provision its own config-less engine and the HTTPS failure returns.

**`make k3s-build` rebuilds EVERY image** (the ray-cluster export alone measures 238 s) when usually
one service changed. (`.docker/frontend.dockerfile` used to copy **all** zones' sources before `bun install`, so touching
one invalidated the install layer of every image. It no longer does — line 56 is
`COPY frontend/microfrontends/${APP} microfrontends/${APP}`, one named zone.)

**The dev loop leaks disk in two places, and both grew past a terabyte before anyone looked.**
`make dev-gc` reclaims both; `make registry-gc` / `make dagger-gc` do one each.

- **The Dagger engine cache.** Its default GC ceiling is proportional to the disk — measured on a
  7.4 TB volume: `maxUsedSpace` **5.67 TB**, `minFreeSpace` 1.51 TB — so it effectively never
  collects. `dagger-engine-rask-state` reached **1.126 TB**. `make dagger-engine` now writes an
  explicit 60 GB cap (`DAGGER_MAX_USED_SPACE` to change it), and the script recreates the engine when
  the CONFIG changes, not only when the version does.
- **The dev registry.** Every rebuild pushes a uniquely tagged image and nothing removes the old
  ones: measured **113 tags of `web-home`**, 83.9 GB. The registry is created with
  `REGISTRY_STORAGE_DELETE_ENABLED=true` — without it `registry:2` refuses DELETE and nothing can be
  reclaimed at all.

Both are safe to wipe: every artefact is reproducible with `dagger call image|zone-image … publish`,
and k3s holds running images in its own containerd cache.

- **`make k3s-up` owns the release.** It renders the chart with `image.localImages=true` (side-loaded
  images). Running `helm upgrade` by hand with different values replaces every deployed image with
  the chart default — which is how a whole fleet lands on `:dev` tags that were never imported.
- **A killed `helm upgrade` leaves the release in `pending-upgrade`,** and every later upgrade is
  refused until someone runs `helm rollback rask <last-good-rev>`. Check `helm history rask` before
  anything else.


`make serve-down` / `make ray-down` to tear down. EAD download: `make harvest-ead` (the `catalog-index` Lance indexer died in the R6/R20 wave — the EAD table re-lands catalog-governed behind `/api/explorer/search`).
(The app database, Alembic migrations and the `pg-*`/`viewer` targets died at P7a — the only relational
store left is the chart-managed `rask-age` Postgres, which serves OpenFGA's database and the Dapr state store.
Lineage keeps no database: its feed and edge list are Lance tables in the catalog's provenance namespace, ADR 0158.)

## Repository layout

Two **language-pure planes** — **don't blur them**. Python lives at the repo root (`packages/` + `services/`); the entire JS/TS estate lives under `frontend/`, its own bun + Turborepo workspace root. (There is deliberately **no Polylith-style `projects/` layer** — it was removed 2026-07; deployables build straight from the root uv workspace via `uv sync --package <name>`, one dockerfile per deployable in `.docker/`.)

- `packages/` — reusable **Python** libraries, **no entrypoints**. uv workspace members.
  - `packages/storage` — the S3 CLIENT seam: `s3_client`/`S3Client`, `configured_endpoint`, `derive_hcp_creds`, `split_s3_uri`/`merge_prefix`, and the `StorageError`/`BucketNotFoundError`/`ObjectNotFoundError` + `s3_errors` family. It still exports `FSSource/Sink`, `S3Source/Sink`, `iter_keys` and `build_source`/`build_sink`, but **those are no longer the fleet's source/sink seam** — this line read as if they were. The lakehouse plane's adapters live in `service_kit.lakehouse.sources` / `.sinks`; `build_source`/`build_sink` and the FS pair have their only production caller in `runners/htr`. **Protocol-agnostic:** a source only every modality can use belongs here; one only a single workload uses belongs in that runner (an IIIF read-through cache lived here until 2026-08-17 with exactly one consumer, and moved to `runners/htr`)
  - `packages/service-kit` — shared **platform library**: `make_service_app` app factory, `Settings`/config, exceptions, middleware, `get_settings`/`SettingsDep`, the injectable lifespan. Dependency-light (no lancedb/ray/sqlmodel).
  - `packages/ray-kit` — Ray Job SDK + dashboard wrapper (schemas, `build_client`, `RAY_TRANSIENT_ERRORS`, the dashboard service). Used by the `compute` service.
  - `packages/validate` — pre-upload image validation: TIFF/JPEG/PNG corruption detection, over bytes or paths. (A `rules.py` rule-plugin framework was exported beside it with no caller and no test anywhere in the estate; deleted 2026-08-29.)
  - `packages/lineage-kit` — the OpenLineage emission kernel and the Ed25519 signing and verifying seam for lineage events (`lineage_kit.signing`), used by catalog, lineage, maintenance, notifications and annotator
  - `packages/ray-cluster-env` — a deps-only member (`package = false`, ships no code) NAMING the Ray images' platform environment (Ray with its data/default/serve extras, plus `service-kit` for the `service_kit.ray_tracing` hooks the head is configured with); both Ray dockerfiles `uv sync --package ray-cluster-env` from the root lock. (`packages/ratch` was DISSOLVED 2026-08-28 — owner ruling, `open_ray-kernel.md`: its Ray-interaction half was a drifted third submission seam retired for `ray-kit`, its schema/dataset layer was vendored into the sealed audio runners that consumed it undeclared, and its CLI had no callers. With it went the one sanctioned CLI exception — packages ship no entrypoints again, without exception.)
- `services/` — runnable **Python** code. **The old monolithic `viewer` service is gone**, and so is the whole batches/orchestrator plane (P7a compute-plane cutover; ADR 0154 and ADR 0155):
  - `services/gateway` — reverse proxy on `:8888` (the frontend's proxy target). Path-routes `/api/*` longest-prefix-first: `/api/catalog`, `/api/lineage`, `/api/explorer/*`, `/api/flows`, `/api/notifications`, and the `compute` rows `/api/ray` + `/api/serve` (app-id `compute`, served by the compute fixture release); owns no state; **no `/api` catch-all** — unmatched `/api/*` 404s. Upstreams are env-overridable (`RASK_COMPUTE_URL` :8804, `RASK_EXPLORER_VIEWER_URL` :8101, `RASK_FLOWS_URL`, `RASK_NOTIFICATIONS_URL`, …).
  - `services/compute` — the **`compute` service** (`:8804`): Ray dashboard introspection (`/api/ray/*`), the `/api/serve/*` proxy and the cron that prunes terminal Ray jobs — the companion of a bring-your-own Ray engine, **not part of the product chart** (ADR 0155). It ships as its own chart, `deploy/fixtures/compute` (release `compute`, installed by `make k3s-fixtures` in rask's namespace), which reads the rask release's Dapr app token, `lance-tracing` Configuration, Dex, OpenFGA, OTel Collector and Ray auth token by reference; rask's OpenFGA admits its service account only when the product chart's `auth.externalFgaClients` names it. The lakehouse runs no stage, no training and no engine, and the import contract `the-lakehouse-knows-no-engine` (`.importlinter`) forbids `catalog`, `lineage`, `maintenance`, `notifications`, `service_kit` and `lineage_kit` from importing `ray`, `ray_kit` or `compute`. Thin shell over `ray-kit`; no DB. `compute` names the uv member, import, image and app-id (R22); its HTTP paths are `/api/ray` + `/api/serve`. (`core`/`core_api`/`search_api`/`volumes_api` died in the R6/R20 media wave — the S3 object browser now lives in the lance `viewer` at `/api/explorer/object*`; lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/explorer/search`.)
  - `services/notifications` — the **targeted inbox** (`:8850`, app-id `notifications`) behind the estate's bell. One Dapr `InboxActor` per subject holding claim-check POINTERS with durable read state, so the badge counts YOUR work rather than the estate's. Two ingresses: a `lineage.events.v1` subscription and a `bindings.cron` reconciler over lineage's durable `GET /events` (the bus alone is provably incomplete — external OpenLineage producers, a user's pipeline among them, emit over HTTP only). SIX targeting sources: authorship, ORIGINATOR (a service-authored run done FOR a named person), `project#member`-gated project watches, the grant pair, and the task pair (assigned/unassigned/changes-requested/dropped). Delivery gates on `can_be_notified`, render on `can_get_metadata` — see `.claude/skills/openfga` and `rask-services-fleet`. **COVERAGE IS DECIDED AT THE PRODUCER, not here:** a state change that names nobody is not under-delivered, it is undeliverable, and `notifiable()` answers an event it cannot target with a SUCCESS ack — so a producer that stamps a role literal in `author.sub`, omits `lance.project`, or emits no event at all fails silently and is reported by nothing. Before adding a feature whose outcome a person should hear about, read `.claude/skills/rask-notifications` (the contract).
  - **The medallion is tier governance, not a service** (ADR 0155). The governed tiers are exactly bronze→silver→gold (R23: raw is the external world, never a governed tier), and ADR 0155 puts their rules in the **catalog's write path**, so no writer can bypass them. Moving data between tiers is the **user's pipeline, on their own compute and orchestrator**: it reads a tier and writes the next through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`) like any other client, and the catalog announces every committed write on lineage; how a pipeline outside the estate consumes those announcements is a separate decision, and the NATS topics are internal. The catalog's gates door declares a project's quality gate and the publish door runs it; nothing holds a promotion for a person. The model registry stays: `models$<name>` are governed tables, promoted candidate→blessed on the catalog's `/management/v1/model/{name}/promote`. There is no protocol-specific ingest door, and adding one would make that protocol privileged.
- `frontend/` — the **JS/TS plane** and its own workspace root: `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/` (the shared favicon source). The only JS outside it is `tests/e2e`, a standalone Playwright project with its own lockfile (`make e2e`).
  - **The 7 zones** (`frontend/microfrontends/<zone>`), SvelteKit 2 + Svelte 5, **SSR** via `svelte-adapter-bun` (a real Bun server: `bun ./build/index.js`), composed by Turborepo's built-in microfrontends proxy on `:3024` in dev and the k3s Ingress in prod: `home` (catch-all, base `''`, owns `/` + the OIDC BFF), `lakehouse` (`/lakehouse` — data/lineage/models/admin/storage areas), `explorer` (`/explorer`, labelled **Explorer**), `annotator` (`/annotator`, labelled **Annotate**), `compute` (`/compute`), `models` (`/models`, the model plane — it REPLACED `train`, on train's port 5178; `train` has zero tracked files and a leftover `microfrontends/train/` or `microfrontends/media/` on a dev host is untracked build residue, not a zone), `studio` (`/studio`). Bases are a bare `/<zone>` — **there is no `/default/` segment**. Every zone renders the shared `@rask/ui/shell` AppShell. See `.claude/skills/rask-frontend`.
  - `frontend/packages/ui` — Svelte 5 + Bits UI + Tailwind 4 design system (`@rask/ui`) w/ Storybook 10 (`@storybook/svelte-vite`). The only frontend package with a build step (`svelte-package` → `dist/`). **Styled components live here, not in the zones** (zones supply `app.css` with an `@source '../../../packages/ui/dist'` — three `../`). 40 subpath exports incl. **`@rask/ui/shell`**. See `.claude/skills/rask-styling`.
  - `frontend/packages/api` — `@rask/api`, the shared data layer: typed gateway client **plus** the OIDC/BFF plane (`bff.ts`, `oidc.ts`) and the lineage client. valibot. JIT TS, no build step.
  - `frontend/packages/explorer-api` — `@rask/explorer-api`, the Arrow-backed explorer/viewer client.
  - `frontend/packages/engine` — `@rask/engine`, a framework-agnostic PixiJS/WebGPU annotation canvas (ra-anno lineage).
  - `frontend/packages/labeling` — `@rask/labeling`, the `LabelOp` model + the annotator's Arrow-IPC transport.
  - `frontend/packages/config` — `@rask/config`, one shared `tsconfig.base.json` (extended by 6 of 14 packages).
  - `frontend/packages/zone-contract` — `@rask/zone-contract`, **imported by nothing at runtime**: 21 files gating the estate's shape (cross-zone `data-sveltekit-reload`, the zone manifest, deploy paths, and a toolchain guard that fails the build if ESLint/Prettier reappear), **plus the dev scripts those gates guard** — `src/dev-zone.ts` (`make dev-zone`) and `src/proxy.ts`. Dev tooling lives *inside* a package on purpose: `lint`/`fmt` are per-package turbo tasks, so a script parked outside both workspace globs would be linted and formatted by nothing.
- `scripts/` — **all** dev/ops scripts, shell + Python: one-shot setup / debug tools (`harvest_ead`, `index_catalog`, `download_*`, `smoke_s3`, …) plus `dev-micro.sh` and `k3s-install.sh`. **No production-state-changing CLIs** — data enters, and moves between tiers, only through the catalog's namespace doors.

- `runners/` — **sealed model environments, NOT workspace members. This is the ONLY place a modality may live.** One directory per workload, each holding its Ray Data pipeline (`src/runner`) *and* its model actors in one project with its **own `pyproject.toml`**; three (`assist`, `dummy`, `htr`) commit their own `uv.lock`, and the other six have none tracked, so their images resolve at build time. Matched by no glob, which is the point: a runner's heavy ML stack (torch, ultralytics, transformers, …) never enters the fleet's resolution (root lock 200 → 145 packages). **The seal's saving is in RESOLUTION and IMPORT, not in the suite's runtime** — measured 2026-09-10, the fleet suite is **~8m20s** over 8,586 tests (two runs agreeing within 0.3%), a broad front with no hotspot: the slowest single test is 14.1 s and the top 30 sum to 34%. Selecting testpaths is the only lever. `storage` is a **path** dep. A runner's tests are invisible to the root pytest — `make test` runs them separately; its images build from **its** lock; it carries its own ruff config (ruff resolves the nearest pyproject). Ray entrypoint: `uv run --project runners/<workload> runner`, overridden in-cluster by `RASK_RUNNER_CMD=runner`. A second workload is a second sibling directory, and needs no change anywhere else. **A workload's awkward dependencies are ITS problem — never solved by fattening a shared image.** **RESOLVED 2026-08-25: PER-WORKLOAD BAKED IMAGES** (owner decision). `.docker/ray-cluster.dockerfile` is the agnostic head image — it builds `packages/ray-cluster-env` (the deps-only platform-env member) from the ROOT lock and installs no runner at all (verified in the built image: torch, htrflow, ultralytics and cv2 absent). One workload's image is built from the PARAMETRIZED `.docker/ray-runner.dockerfile` via `ARG RUNNER`, exactly as `frontend.dockerfile` builds all seven zones from `ARG APP`: `scripts/dagger-image.sh --runner <runner> --tag ray-<runner>:<tag>`. A Serve application declares its own image as `runtime_env.image_uri` in the engine's Serve config (the rask chart renders no Serve application, ADR 0153), so several workloads share one cluster without sharing one environment. A tenth runner is a new `--runner=` value and edits no file. Note the distinction, because the word is overloaded: the 2026-08-23 ruling rejects shipping DEPENDENCIES through `runtime_env` (its `pip`/`uv` fields, resolving a stack at replica start), and that still stands. `runtime_env.env_vars` (scoping ENVIRONMENT to one application or job) and `runtime_env.image_uri` (naming a prebuilt image) are both fine and are how this is wired. **The two environments do not merge, and that is the seal working:** a runner image's default interpreter is the workload's venv, so `import lance` fails there by design — the head image bakes no job and carries no Lance plane either (ADR 0155: a job that writes a tier is the user's, in the user's image). No offline test pins that a declared application's import path exists in the image that serves it: the check belongs in a Dagger smoke over the built images (import each application), and that smoke does not exist yet.

**Workspace membership is globbed, per plane** — the directories are language-pure, so every child carries the right manifest:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["microfrontends/*", "packages/*"]` (relative to `frontend/`)

A new Python library/service or a new zone is picked up by the glob — but it **must** ship a `pyproject.toml` (Python) or a `package.json` (JS), or its plane silently drops it.

Deployables are just workspace members with a dockerfile: `.docker/<name>.dockerfile` runs `uv sync --frozen --package <name>` against the **root** `uv.lock` (ten Python services: `annotator`, `catalog`, `compute`, `flows`, `gateway`, `lineage`, `maintenance`, `notifications`, `search`, `viewer`. `compute`, `flows`, `gateway` and `notifications` each build their own `.docker/<name>.dockerfile`, and every image but `compute`'s is deployed by the product chart; one image, `.docker/rest-catalog.dockerfile`, installs and serves `catalog`, `lineage`, `maintenance`, `viewer`, `search` and `annotator`; `.docker/runner.dockerfile` is the runner image and the Ray cluster image is `.docker/ray-cluster.dockerfile`).

## Architecture

**`rask` IS AN AGNOSTIC, MULTIMODAL LAKEHOUSE. Any pipeline, on any compute and orchestrator, reads and
writes its governed tables through the catalog.** That is the whole of what it is. A Lance lakehouse under Lance
Namespace (catalog + provenance), events on Dapr + NATS JetStream, with lineage and authorization across all of it;
the compute engine and the workflow engine are the user's (ADR 0154, ADR 0155).
Any modality — text, image, audio, video, embeddings, or one nobody has written yet — is a first-class
citizen: bytes in, a governed table out, lineage emitted. That is observable, not aspirational: there are
**nine sealed runners** (`asr`, `assist`, `diarize`, `dummy`, `htr`, `insid3`, `kg`, `topics`,
`voiceprint`), spanning audio, image and text. No single one of them is the platform, and if any file
here reads as though one is, that file is wrong.

**It is NOT an HTR system, and it belongs to no institution.** Never describe it as either. A data type
must never enter a shared seam — the catalog, tier governance, the sweep, annotation, search, lineage,
notifications — and the test for every one of those is *would this be right for audio?* If an answer
needs a modality's name, it belongs in a sealed `runners/<workload>`, whose internals (stage graph,
models, GPU packing, output format) this file deliberately does not describe. An organisation name is
only ever an external identifier someone else owns — a model repo, a source URL, the GitHub org — never
a description of what rask is.

Key facts that aren't obvious from any single file:

- **LANCE ONLY, ALWAYS — a permanent ruling (owner, 2026-08-15), not a current-scope note.** The catalog
  stores Lance tables and **no other format, ever**. A create naming a non-Lance format is refused 400
  at the door (`core/formats.py::reject_unsupported_properties`; pinned by `tests/unit/test_format_guard.py`) —
  that 400 is the final answer, not a gap awaiting Parquet/Iceberg/Delta support. This is load-bearing,
  not a preference: the catalog is deliberately format-AWARE (imports pylance, serves the data plane
  in-process, coordinates commits), and all three are only sound because the format is closed. It is
  also why the estate needs no relational DB — Iceberg puts the commit pointer IN the catalog so every
  commit is a DB transaction, while Lance puts the CAS in the object store. A second format would
  reintroduce exactly the requirement this architecture is built to avoid. Anyone needing Iceberg
  interop should use Polaris or Lakekeeper; it is a road not taken, not a backlog item.

- **A Lance APPEND never names a file version.** A create names `data_storage_version="2.2"` and
  `enable_stable_row_ids=True` (both create-time-only through pylance); an append inherits the table's
  version. On pylance 12 and 13 an append or fragment commit whose files are at another version is
  COMMITTED and stamps reader flag 256 (mixed data file versions), which no operation removes:
  maintenance refuses the table, and pylance 11 and lancedb 0.34 cannot open it, while lancedb 0.40 (the
  pinned one) can (`lance_docs/PROVENANCE.md`). The catalog's `version/create` (a stock client's commit, ADR 0157) and the register door refuse
  such a set (`service_kit.lakehouse.features.describe_foreign_data_file_versions`), and the maintenance
  counter `compaction.datasets.mixed_file_versions` pages on one. pylance 13's `update()`,
  `merge_insert().data_storage_version()` and `compact_files()` also take a `data_storage_version` that
  can stamp the flag: a stock client's such commit is refused at `version/create`, and rask code never passes it.
- **A workload is a SEALED RUNNER, and the platform knows nothing about it.** `runners/<workload>` submits one Ray Data pipeline per CLI invocation and blocks on `.materialize()`; long-lived model weights stay warm behind **Ray Serve** deployments the runner owns and `make serve-up` deploys independently of any job. Serve replica counts and GPU fractions are the RUNNER's business (`RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC` plus literals inside its own pipeline module) — never the platform's. Whatever a workload's stage graph, model or output format is, it reaches the platform as config: bytes in, a governed table out, lineage emitted. Its per-workload mechanics live in that runner's own README, not here.
- **The estate is AUTHENTICATED, and this bullet used to say the opposite.** It read *"No auth, no app
  middleware. The services assume localhost / trusted network"* — which was true once and has been
  false for a while: `chart/values.yaml` defaults auth ON, nine services mix in `GovernedAuthSettings`,
  and `chart/templates/ingress.yaml:66` publishes `/api` at the edge. The stale sentence was not
  harmless: read as policy, "trusted network" made an unguarded service look deliberate rather than
  unfinished. Owner ruling 2026-08-26: authenticated is the truth, the ungated services get the door
  their siblings share, and body caps + rate limiting land as one seam in
  `service_kit.middleware.register_middleware` rather than per route. That work landed: the tracking
  file, `open_fastapi-audit.md`, was **DRAINED AND DELETED 2026-08-28** — all 55 findings fixed
  RED-first, every closure adversarially re-verified (the drain's own commits carry the record).
  **`compute` is gated too** — verified 2026-09-09: it mixes in `GovernedAuthSettings`, ships a
  `security.py` whose `require_read` resolves a `CurrentSubject` plus an FGA checker, and gates the WHOLE
  `/ray` router (`dependencies=[Depends(security.require_read)]`). The frontend
  hits `/api/*` on the **gateway** (`:8888`), which path-routes to the per-domain services; its `/api/ray/*` and
  `/api/serve/*` rows route to the **compute** service (over `ray-kit`, in its own
  fixture chart) — the URL namespace names the Ray cluster, not the service (R22). SSR `load`/remote functions reach the gateway server-side via an absolute base URL (`RASK_GATEWAY_URL`); client code uses the relative `/api/*` proxy. The gateway sits **behind** the SvelteKit Bun server (it does not serve the SPA shell).
- **State surface:** the lakehouse (Lance datasets on S3; MinIO in development, `rask-minio`) governed by the catalog, plus the chart-managed **OpenFGA** Postgres database and the Dapr state store — served by the `rask-age` **StatefulSet**, NOT by CloudNativePG. Lineage keeps no database (ADR 0158): its commit feed (`provenance$events`), table-level edge list (`provenance$edges`), outbox refusals and forwarder cursors are Lance tables in the catalog's provenance namespace, written and read through the catalog's doors, and the lineage graph is bring-your-own (the relay forwards every recorded event as OpenLineage to `services.lineage.forward.url`; Marquez in development). Measured live 2026-09-07: **zero** `cluster.postgresql.cnpg.io` objects exist, and `age.cnpgCluster.enabled` defaults **false** because the AGE-on-CNPG cutover needs a built extension image (`.docker/cnpg-age-ext.dockerfile`), K8s 1.33+ and CNPG >= 1.27 — the two paths are mutually exclusive and `age-cluster.yaml` fails the render if both are on. The CNPG **operator** is a prerequisite (ADR 0153; in development the `cloudnative-pg` fixture in `cnpg-system`) and runs with nothing to reconcile, so a running operator is not evidence that a `Cluster` exists. **The app's own relational DB is gone** (P7a): no batches table, no Alembic, no `DATABASE_URL` in the fleet. **No Redis**; events ride Dapr pub/sub on NATS JetStream. (`.docker/` still carries 3 `docker-compose*.yml` files: the local stack and two auth/dex variants. They are side stacks for local bring-up, not the deploy path; the Helm chart is.) The Helm chart in `chart/` is the single deploy artifact for both local k3s and production, and installs no operator: the External Secrets Operator, the Dapr control plane, NATS JetStream and the CloudNativePG operator are prerequisites, installed in development from `deploy/fixtures`, each its own release; the chart renders a CNPG `Cluster` only with `age.cnpgCluster.enabled`, for a cluster that serves `postgresql.cnpg.io/v1`. Ray is not in the chart (ADR 0153), and neither is its companion `compute` service (ADR 0155): the engine is bring-your-own, named by `ray.dashboardUrl`, and `make k3s-fixtures` installs a dev one from `deploy/fixtures` (kuberay-operator, ray-cluster, compute), each its own release. Local deploy: `make k3s-install` (one-time) → `make k3s-build` → `make k3s-import` → `make k3s-up`; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Observability (optional, `observability.enabled`):** rask ships ONE thing: the **OTel Collector** (`chart/templates/otel-collector.yaml`, app-id `rask-otel`), its telemetry seam. **Vector was RETIRED 2026-07-27** (owner ruling): the Collector is the single log shipper — its filelog receiver tails infra-pod logs, and it scrapes the Dapr sidecars and control plane and OpenFGA. Every plane (fleet incl. the gateway, Dapr, OpenFGA) exports plain OTLP/HTTP to the Collector and names no store; the Collector exports to **`observability.export.endpoint`** with the backend's headers from `observability.export.*` (GreptimeDB's db-name, trace-pipeline and audit-table headers by default) and basic auth from the store's `otel-collector-export` secret. **The store, dashboards and alerting are the platform's, not the chart's** ([[XC-127]], ADR 0153): the alert rules are an artefact of their own, `deploy/alerting/rules.yml` (promtool-proven by `make alert-rules-check`, replayed against GreptimeDB by `make alert-rules-drill`), for the platform's evaluator. Development gets the chain from the `observability` fixture (`deploy/fixtures/observability`, namespace `observability`): GreptimeDB (`greptimedb.observability:4000`, local volume, retention Job) → Perses (rask's dashboards) and vmalert → Alertmanager, which `deploy/values-local.yaml` names; the zones' audit viewer and experiment curves read the store at `observability.query.url`. Standard OTLP throughout (not OTel-Arrow).
- **The lakehouse runs no ingest, no stage, no training and no engine (ADR 0154, ADR 0155).** Data enters through the catalog's namespace doors and the catalog announces each committed write; nothing inside the lakehouse reacts to that announcement by copying rows into the next tier — a user's pipeline does, on its own compute. Ingest of an external source is a bring-your-own client job (a sealed runner, CP-061, not yet built) that commits through the catalog; until it lands, data reaches bronze only from a client speaking the namespace doors, and development and the e2e lanes seed bronze through the catalog's `insert` (`tests/e2e-py/bronze_seed.py`); a create alone is a DDL event. **No tier carries a workload-shaped schema**: a governed row needs no column, because its run and inputs ride the commit as `rask.*` transaction properties (ADR 0156 C9, LH-225); `source_rowid` is the one column the platform names (`service_kit.lakehouse.stage_stamp`), an opt-in held to uint64 stable row ids and reserved by name in the catalog (`catalog/core/provenance_guard.py`), and the rest of the row, `stage` and `lineage` included, is the writer's. A workload's own output shape belongs to its sealed runner or the user's pipeline.
- **Source bytes are a WORKLOAD concern.** A runner may read from S3, an HTTP endpoint, a read-through cache, anywhere — the platform takes data only through the catalog's namespace doors and does not care where the bytes came from. `packages/storage` supplies the source/sink adapters; the platform has no privileged ingress for any particular protocol.

## Conventions

- **Gateway port is 8888 — but the zones' dev proxies disagree on who to call.** `compute`/`studio`/`models` proxy `/api` → `VIEWER_BACKEND` (`:8888`, the gateway); `home`/`lakehouse` proxy → `LANCE_BACKEND` (**`:8001`**, the lineage service — which `dev-micro.sh` does not start); `explorer`/`annotator` have no `/api` proxy and reach `:8101`/`:8102`/`:8103` via their own BFF. The same split exists server-side: `compute` reads `RASK_GATEWAY_URL`, `home`/`lakehouse` read `LANCE_GATEWAY_URL`. A `/api/*` call that works in one zone can fail in another — see `.claude/skills/rask-frontend`.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are globbed (`testpaths` names `packages/*/tests`, `services/*/tests` and the three root suites), not discovered from the rootdir.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **oxfmt uses tabs**, single quotes, `printWidth: 100` — defined in `frontend/.oxfmtrc.json`, applied across every JS/TS workspace (zones, `@rask/ui`, `@rask/api`, `@rask/zone-contract`). Prettier is gone.
- **JS monorepo runs on Turborepo** (`frontend/turbo.json`): `bun --cwd=frontend run build`/`check`/`dev` delegate to `turbo run` (package tasks + `^build` ordering + cached `build`/`.svelte-kit`/`dist` outputs). Add a new JS package's scripts in its own `package.json` — never centralize task logic in root. `lint`/`fmt`/`fmt:check` are **per-package turbo tasks** too (each package runs `oxlint` / `oxfmt`); only `knip` stays root-level, because it analyses the whole JS graph at once.
- **The cross-zone link gate is a test, not a lint rule.** A cross-zone `<a>` must carry `data-sveltekit-reload` or SvelteKit soft-navigates into a route the zone doesn't own (→ 404). Enforced by `@rask/zone-contract`'s vitest suite (`frontend/packages/zone-contract/src/cross-zone-reload.test.ts`) — oxlint's `.svelte` support reads the `<script>` block, not the markup, so an anchor-attribute rule cannot live there.
- **Frontend is SSR + Svelte 5 strict.** Every `.svelte` change is validated with the Svelte 5 skills + the `svelte` MCP autofixer. Browser-only globals must stay inside `onMount`/`$effect`/handlers (never component top level or `load`) or SSR render crashes.
- **`ty` is configured with `error-on-warning = true`** — typecheck warnings fail CI.
- **Comments carry rationale and provenance, never HISTORY** (owner ruling 2026-08-30). Three kinds of
  prose live in these modules and only two may stay: **(1) rationale** — why it is this way, what is
  load-bearing — stays; **(2) provenance** — "measured 2026-08-26", "pinned by `test_x.py`" — stays,
  with its measurement; **(3) history** — "this used to say X", "an earlier version claimed Y" — is
  **banned in new code**. Falsified prose is REWRITTEN, and rewriting means the old claim is gone, not
  annotated as wrong. Where the history genuinely matters, it belongs in a new ADR under `docs/adr/` or
  the commit message, both of which are built to hold it.
  **The rule is FORWARD-ONLY and that is deliberate**: the ~35,000 existing prose lines are not to be
  drained, because a retroactive pass is a large unreviewable diff that would delete the measurements
  the prose exists to preserve. `scripts/comment_history_gate.py` enforces it on CHANGED lines only;
  its `--report` mode is an advisory whole-tree count and never gates. Passing the gate is a floor
  under the rule, not proof of conformance — only a reader can tell the three kinds apart.

## Agent skills

### Issue tracker

GitHub Issues on `AI-Riksarkivet/rask`, one issue per row (`LH-243: …`), ordered by the pinned `Phase 1 map` issue. See `docs/agents/issue-tracker.md`.

### Workflow (the mattpocock skills, plugin `mattpocock-skills`)

Work moves idea → ship along one path, and `/ask-matt` explains any step:

1. `/grill-with-docs` sharpens an idea by interview and records terms in `CONTEXT.md` and decisions as ADRs.
2. `/to-spec` writes the spec as an issue; `/to-tickets` splits it into rows with native blocking edges.
3. `/implement <#row>` builds one row: `/tdd` red-green slices at the seams, the type checker and single test
   files as it goes, the full suite once, then `/code-review` (Standards and Spec reviewers in parallel; one
   round, HIGH and MEDIUM fixed), then a commit. `/clear` between rows.
4. `/triage` admits rows nobody on the team wrote; `/diagnosing-bugs` takes a hard bug to a red test first.

**Every row, whoever works it, follows the collaboration rules in `docs/agents/issue-tracker.md`:**

- **Claim first.** `gh issue edit <n> --add-assignee @me` is the first write, before a branch or a test. An
  assigned row is its assignee's; take the next one from the map's frontier (first unassigned, unblocked row).
- **One row, one commit, straight to `main`** (owner, 2026-10-14: one person works the repo, and a CI round costs
  30-40 min for nothing the local gates miss). Commit the row on `main` as one commit, run the local gates (the full
  offline suite, `ruff`, `ty`, `make lint-imports`, the comment gate, `make openapi` when a route changed), push, then
  deploy and read back. No PR.
- **Reference a row with `Refs #n`, never a closing keyword.** `Closes`/`Fixes` in a commit or a PR body closes the
  issue on push or merge, and a row closes only when its fix is deployed and read back live: close it by hand
  with a comment naming the helm revision.

### Triage labels

The five default roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.

## Claude Code project config

- All project-local config lives under `.claude/`. **No `.mcp.json` at repo root** by design — the svelte MCP server is registered at `local` scope via `make claude-bootstrap` (idempotent). The install command in the `Makefile` is the source of truth for which MCP servers this project needs.
- `.claude/settings.json` is committed (team-shared: `enabledPlugins`, `extraKnownMarketplaces`, permissions, hooks). `.claude/settings.local.json` is gitignored (personal overrides + local-scope MCP).
- **Shared skills come from the [`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills) marketplace** (language/toolchain: writing-python, fastapi, dagger, dockerfile, otel, testing-python, turborepo, zensical-_, …) — not vendored; `make claude-bootstrap` installs them, and you change one by editing it in ra-skills. **rask's own project skills are vendored in `.claude/skills/`** — they describe rask internals and evolve with the code, so edit them in place (the same way ra-hcp keeps its `hcp-*` skills local). Route by plane:

| Working on | Skill |
| --- | --- |
| Where code belongs; workspace globs; `pyproject.toml`; a new member or deployable | `rask-architecture` |
| The gateway, an endpoint's route, a 404/502/403 through `/api/*`, ports | `rask-services-fleet` |
| A zone, a route, data fetching, cross-zone links, the frontend gates | `rask-frontend` |
| `@rask/ui`, tokens, `class=`, an unstyled page, a new component | `rask-styling` |
| Whether a feature should tell a PERSON something — emitting a run/control event, a notification that never fired, a new `ControlAction`/`NotificationReason` | `rask-notifications` |
| A Dapr component vs hand-rolling — queuing, scheduling, locking, config, secrets, middleware; which blocks this estate uses/refused and why | `rask-dapr` |
| An authorization model, tuples, `.fga` files | `openfga` |
| Writing, changing, deleting or auditing any test; a test audit or prune | `testing-python` + `rask-testing` |
| Anything under `chart/`, a subchart or CRD, `helm upgrade`, a value that never reaches a pod, the 1 MiB release ceiling | `rask-helm` |

These skills are maintained against the code and **will drift** — when you find a claim that contradicts a file, fix the skill in the same commit as the code.
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
