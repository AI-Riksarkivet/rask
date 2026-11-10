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
  ruff or an import-linter contract, or nowhere. Measured 2026-11-09 (`pytest --collect-only`): 3,844 tests in
  557 files (98k test lines against 77k production lines); measured 2026-09-28, before XC-192, 2,743 of the 12,066
  tests then collected (22.7%) executed no production line at all.
- **No silent scope-cuts.** If you bound coverage, sample, or defer something, say so
  explicitly. Don't let "partially done" read as "done".
- **No backward compatibility — the estate is pre-production.** No migration shims, dual read paths,
  deprecation windows or "keep the old key working": change the thing and move every consumer in the
  same commit. Current state is test data.
- **Lance-idiomatic, answered from `lance_docs/`, never guessed.** A question about Lance or
  lance-namespace behaviour is settled by citing `lance_docs/` (upstream `docs/src` of lance in `lance/` and of
  lance-namespace in `ns_catalog/`, pinned in `lance_docs/PROVENANCE.md`; `ns_catalog/spec.yaml` is authoritative over
  the namespace prose, and the docs run ahead of the installed pylance, so a claim rask relies on holds on it) or by
  measuring on the installed pylance — never from memory.
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
  **FGA/governance**, **maintenance**, and the **events** they emit (the notifications inbox that read them is retired,
  owner 2026-11-06: "Drop it, like all three", ADR 0193; the events and their targeting contract stay for any notifier a
  client builds). Compute comes second. Search, the viewer (explorer), the annotator and flows are
  ignored for now; the models zone and Kueue are decoupled and LOW. A cross-cutting or infra row (chart, CI,
  Kueue) is taken only as the stated enabler of a named Phase 1 row.
- **Owner decisions go through the multi-question tool**, each option with a concrete what, how and
  why — never as a question trailing a report.
- **Stopping rules (owner, 2026-10-05).** When a step doesn't need the owner's input, keep going. Put
  status notes in the same message as the next action. Stop and ask only when you cannot continue without
  the owner, or before anything destructive. Every delegated agent's report marks what it could not
  confirm and where it looked; a reviewer lists only problems it would block the merge for, each with
  the file and line, why it is wrong, and how to show it fails.
- **Four facts judge every design, GitOps first (owner, 2026-10-12 and 2026-10-18): GitOps, cloud native,
  Lance-namespace/table-idiomatic, zero trust, always.** Put them in every agent brief, every mattpocock skill step
  (`/grill-with-docs`, `/to-spec`, `/to-tickets`, `/implement`, `/tdd`, `/code-review`) and every option, naming the fact
  each one meets or breaks, before offering it. **GitOps**: the estate is what git declares. Every object, value,
  identity and policy is rendered from the repo and converges when a reconciler (Argo CD, the planned rask
  operator) re-applies it, with no imperative step, live-only state or hand-run command that an install from git would
  miss, and drift is corrected by the next reconcile. **Cloud native**: platform services are named, never bundled
  ("Platform services are external" below), and every component is a declarative Kubernetes workload.
  **Lance-namespace/table-idiomatic**: data is reached through lance-namespace's doors and Lance's own table primitives,
  settled from `lance_docs/` ("Lance-idiomatic" above). **Zero trust**: no identity trusted by position, no secret
  through env, every credential scoped and short-lived, and the store refuses what a credential was not granted. A change
  whose purpose is closing a zero-trust gap (an unsigned writer admitted, a credential that never rotates, an identity
  trusted by position) is pre-approved: do it and report it. Still ask before anything destructive on shared live
  state, and still ask a real trade-off between two designs that both meet the four facts.
- **The lakehouse names no client, anywhere** (owner, 2026-11-04: "Lakehouse don't know anything about the other stuff what so ever. It does not eg know flows or compute"). No file the lakehouse owns (the product chart, `deploy/values-*`, its alert rules, the platform dev fixtures, the `lakehouse` library, catalog/lineage/maintenance, their tests) names a client; `scripts/lakehouse_names_no_client.py` enforces it in `make lint` and `dagger call lint` ([[XC-191]]), over the console's tree too. **This repository hosts no client** (owner, 2026-11-06: "Console in product", ADR 0193): the console is the lakehouse's own UI, shipped by the product chart with its own settings and its own sign-in client, and the notifications inbox is retired. The lakehouse ships only the generic seam, with empty defaults: `openbao.externalReaders`, `networkPolicy.objectStoreExtraFrom`, `nats.auth.users`, the Dex fixture's `clients`. A client's roles on the catalog are no value at all: they are grants to its own identity made at the catalog's access doors, which no upgrade or boot revokes ([[LH-563]]). A client's integration values (its Dex sign-in client, its store peer) are written in the client's own repository and supplied to a lakehouse install from there (`-f <client values>`, or `Lakehouse(values, dexClient, …)` for its e2e). The removal test holds for files as well as imports: deleting every client changes no lakehouse file.
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
  (no `opus-medium` agent type is defined: `general-purpose` with model `opus` at medium effort); read-only sweeps may use Sonnet. A review round fixes every finding in code the row
  added or changed, LOWs included (owner, 2026-10-26: a LOW parked in a row's own code costs a row later), and parks
  only findings about code the row did not touch; one round is the default. A worktree agent starts from `origin/main`,
  so pin its base with `git switch -c <branch> <sha>` when local `main` is ahead.
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
  anything in `.docker/` (`--name=frontend` is the console). **`docker build`
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
- **The JS/TS plane lives in `frontend/`** — its own bun workspace root (its own `package.json`, `bun.lock`). Every bun call is **scoped to it**: `bun --cwd=frontend run <task>`. Use the `--cwd=` form — `bun --cwd <path>` with a space silently no-ops.
- **JS/TS lint + format is oxlint + oxfmt**, not ESLint/Prettier (both deleted). Svelte support comes from `@rsvelte/oxlint-plugin` (lint) and `@rsvelte/fmt` (format); configs live at `frontend/.oxlintrc.json` and `frontend/.oxfmtrc.json`. `lint` / `fmt` / `fmt:check` are per-package scripts the workspace root runs over every package (`bun run --filter '*'`), from `frontend/`.
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uv run ty check` (the locked ty; `uvx ty` resolves the newest release and drifts from the lock).
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal                         | Command                                                                  |
| ---------------------------- | ------------------------------------------------------------------------ |
| First-time setup             | `make install` (= `bun --cwd=frontend install` + `uv sync --all-packages` + git hooks + `make k3s-deps` (subcharts) + `make claude-bootstrap`, which installs the ra-skills and mattpocock plugins) |
| Build everything             | `make build`                                                             |
| Run tests (excludes slow, e2e) | `make test` (= `uv run pytest -m "not slow and not e2e" -n 16`)        |
| Run all tests incl. slow     | `make test-slow`                                                         |
| Single Python test           | `uv run pytest services/catalog/tests/<file>.py::test_name`             |
| Filter by name               | `uv run pytest -k <pattern>`                                             |
| Skip slow tests              | `uv run pytest -m "not slow"`                                            |
| Format + lint + typecheck    | `make check` (= `make fmt` + `make lint` + `make lint-imports` + `make typecheck` + `make knip` + `make fga-test` + `make go-fmt` + `make chart-docs-check`) |
| Scan dependencies for CVEs   | `make audit` (osv-scanner over every lockfile + `.dagger/go.mod`)        |
| Scan dockerfiles + chart     | `make scan-config` (trivy misconfig + secret detection)                  |
| Scan git history for secrets | `make scan-secrets` (trufflehog; gates on VERIFIED credentials only)     |
| Scan a built image           | `make scan-image NAME=rest-catalog` (`NAME=frontend` is the console)     |
| Frontend gates               | `make frontend-check` (check, lint, fmt:check, unit tests, knip); e2e `bun --cwd=frontend run test:e2e` |
| Storybook for `@rask/ui`     | `make storybook` (→ `:6006`)                                             |
| Bootstrap Claude Code config | `make claude-bootstrap`                                                  |

### Run the app locally

```bash
make dev               # the console with Vite HMR on :5173, /config.json from frontend/console/dev/config.json, /api proxied to RASK_EDGE_URL (default :8080)
make dev-k3s           # the same against the IN-CLUSTER edge and Dex (port-forwards k3s Traefik's Service to :8080, or reuses :8080 when it already serves /dex)
```

**The console is one static single-page app with no server of its own** (`frontend/console`): the browser signs in to
Dex with PKCE on the lakehouse's public client `rask-console`, keeps its tokens in memory, and calls the lakehouse's `/api/*`
with its bearer, same-origin through the Gateway. The product chart ships it (`chart/templates/console.yaml`, ADR 0193). `make dev` therefore needs an edge on
`RASK_EDGE_URL` that also serves the issuer `frontend/console/dev/config.json` names (`http://localhost:8080/dex`,
client `rask-console`; `CONSOLE_CONFIG_FILE` names another file): `make dev-k3s` provides the cluster's. Its Playwright suite needs none: it serves the BUILT app behind
`frontend/console/e2e/edge.ts` with mocked upstreams and a mock OIDC issuer (`e2e/mock-oidc.ts`), so the real PKCE
path runs. See `.claude/skills/rask-frontend`.

### The in-cluster loop: k3s + k9s

`make dev` cannot reproduce anything that only manifests IN-CLUSTER — Dapr sidecar injection,
the catalog's commit announcements reaching lineage's durable feed over the bus, FGA checks. For those, build and deploy:

```bash
make k3s-up          # the cluster + release (one-time per session)
make dev-registry    # ONCE per host: registry on :5000 + point k3s at it (sudo; restarts k3s)
make dagger-engine   # ONCE per host: a Dagger engine that may push to that (plain-HTTP) registry
make k9s             # inspect the cluster (installed into .localbin by `make bootstrap`)
```

Images are built by Dagger and pushed to that registry:
`dagger call image --name=rest-catalog publish --address=172.17.0.1:5000/lance-rest-catalog:<tag>`, then
`kubectl set image` or a chart value. **For pure UI work `make dev-k3s` is the faster loop** — Vite HMR, sub-second,
against the cluster's edge.

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

**`make k3s-build` rebuilds EVERY image** (the lakehouse's and the console's) when usually one changed;
`scripts/dagger-image.sh --name <stem>` builds one.

**The dev loop leaks disk in two places, and both grew past a terabyte before anyone looked.**
`make dev-gc` reclaims both; `make registry-gc` / `make dagger-gc` do one each.

- **The Dagger engine cache.** Its default GC ceiling is proportional to the disk — measured on a
  7.4 TB volume: `maxUsedSpace` **5.67 TB**, `minFreeSpace` 1.51 TB — so it effectively never
  collects. `dagger-engine-rask-state` reached **1.126 TB**. `make dagger-engine` now writes an
  explicit 60 GB cap (`DAGGER_MAX_USED_SPACE` to change it), and the script recreates the engine when
  the CONFIG changes, not only when the version does.
- **The dev registry.** Every rebuild pushes a uniquely tagged image and nothing removes the old
  ones: measured **113 tags of one web image**, 83.9 GB. The registry is created with
  `REGISTRY_STORAGE_DELETE_ENABLED=true` — without it `registry:2` refuses DELETE and nothing can be
  reclaimed at all.

Both are safe to wipe: every artefact is reproducible with `dagger call image … publish`,
and k3s holds running images in its own containerd cache.

- **`make k3s-up` owns the release, and composes its values from git** ([[XC-194]]): `--reset-values` with
  `deploy/values-local.yaml`, the registry `K3S_REGISTRY` (default `localhost:5000`; empty for side-loaded
  images), the tags in `deploy/values-live-pins.yaml`, and the derived issuer, vended endpoint and bootstrap admin.
  It reads nothing back from the release, so a value set only on the live release is gone after the next run;
  `make k3s-values` prints what it supplies. A new build reaches it by tag:
  `make k3s-pins TAGS="lance-rest-catalog=<tag> web-console=<tag>"`, then `make k3s-up`. Running `helm upgrade` by
  hand with other values replaces every deployed image with the chart default.
- **A killed `helm upgrade` leaves the release in `pending-upgrade`,** and every later upgrade is
  refused until someone runs `helm rollback rask <last-good-rev>`. Check `helm history rask` before
  anything else.


(The only relational store in the lakehouse is the chart-managed `rask-age` Postgres, which serves OpenFGA's database.
Lineage keeps no database: its feed and edge list are Lance tables in the catalog's provenance namespace, ADR 0158.)

## Repository layout

This repository holds **the lakehouse and its console, and nothing else** (owner, 2026-11-04; XC-192). Every client
(flows, compute, the annotator, the viewer and search, table-maintenance, the sealed runners and their zones) was
deleted here and is recovered from git history at `64ef118a9` when it moves to a repository of its own. The
notifications inbox is retired, not moved (ADR 0193).

Two **language-pure planes** — **don't blur them**. Python lives at the repo root (`packages/` + `services/`); the JS/TS
plane lives under `frontend/`, its own bun workspace root. There is no Polylith-style `projects/` layer: deployables
build straight from the root uv workspace via `uv sync --package <name>`, one dockerfile per deployable in `.docker/`.

- `packages/` — reusable **Python** libraries, **no entrypoints**. uv workspace members.
  - `packages/lakehouse` — **the lakehouse's one library** (import `lakehouse`, ADR 0195), imported only by its own
    services (`catalog`, `lineage`, `maintenance`) and by no client: everything they run on, organised by module (ADR 0199 decides ten subpackages under a layers contract, not yet moved). The
    lance app factory (`lakehouse.lance_app`) and what it installs: exceptions, the request-id middleware, logging
    (`setup_logging`), the probes and the drain, the body cap; the auth plane (`lakehouse.governed`); the naming and
    the spec's error mapping (`naming`, `ns_errors`, `spec_routes`, `endpoint_scheme`); the input and credential guards
    (`arrow_ipc`, `storage_options`); the catalog's records, registries and governance guards (protection, trash,
    history fences, quality gates, retention); the table layout and feature checks; the namespace clients, readers and
    writers (`lakehouse.lancekit`); the events (`control_events`, `control_emit`, `signature_doors`, `lineage_events`,
    `governed.subjects`, the outbox seams with the Dapr publish, the OpenLineage builders in `openlineage`); the
    OpenLineage emission and the Ed25519 signing and verifying seam (`lakehouse.lineage`, `lakehouse.lineage.signing`);
    the store session; and the S3 client, STS and URI seam (`lakehouse.storage`: `s3_client`/`S3Client`,
    `configured_endpoint`, `sts_client`, `iter_keys`, `split_s3_uri`, the `StorageError` family). A client reaches the
    lakehouse through its doors and events, never by importing this package: the doors are `docs/*-openapi.json`, the
    events `docs/COMMIT-EVENTS.md`. The suite's process-global seams (the secret-read cache, the projected token files,
    the bounded Dapr handshake, strict respx) are autouse fixtures in the root `conftest.py`.
- `services/` — runnable **Python** code:
  - `services/catalog`, `services/lineage`, `services/maintenance` — **the lakehouse**, built from the lance app factory
    and shipped as ONE image (`.docker/rest-catalog.dockerfile`) run with three commands.
  - **Telling people is a notifier's, and the lakehouse runs none** (ADR 0190, ADR 0193). Its events and their published
    targeting contract (`docs/COMMIT-EVENTS.md` § Telling people) serve any notifier a client builds. **COVERAGE IS
    DECIDED AT THE PRODUCER:** a state change that names nobody is not under-delivered, it is undeliverable — so a
    producer that stamps a role literal in `author.sub`, omits `lance.project`, or emits no event at all fails silently
    and is reported by nothing. Before adding a feature whose outcome a person should hear about, read
    `.claude/skills/rask-notifications` (the producer's contract).
  - **The medallion is tier governance, not a service** (ADR 0155). The governed tiers are exactly bronze→silver→gold
    (R23: raw is the external world, never a governed tier), and ADR 0155 puts their rules in the **catalog's write
    path**, so no writer can bypass them. Moving data between tiers is the **user's pipeline, on their own compute and
    orchestrator**: it reads a tier and writes the next through the catalog's Lance Namespace doors (`CreateTable`,
    `InsertIntoTable`, `MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`;
    `lance_docs/ns_catalog/spec.yaml`) like any other client, and the catalog announces every committed write on lineage.
    The catalog's gates door declares a project's quality gate and the publish door runs it; with review declared on the
    gate, a breach is held for a `can_promote` holder to approve or reject (ADR 0161). The catalog serves no model
    registry (LH-558). There is no protocol-specific ingest door, and adding one would make that protocol privileged.
- `frontend/` — the **JS/TS plane** and its own bun workspace root: `package.json` (plain bun scripts, no Turborepo),
  `bun.lock`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`.
  - `frontend/console` — **the console**, the lakehouse's own UI, shipped by the product chart
    (`chart/templates/console.yaml`, `console.enabled`; ADR 0193): ONE SvelteKit 3 + Svelte 5 single-page app
    (`adapter-static`, `ssr = false`, fallback `index.html`) with no backend of its own, Lakekeeper-style. The browser
    signs in with OIDC and PKCE on the lakehouse's public client (`console.oidc.clientId`, `rask-console`; the dev Dex
    declares it under its own `console` value), with `oidc-client-ts`, tokens in memory and only the transient PKCE
    state in sessionStorage. It reads its issuer, client and scopes from `/config.json` at boot (the chart's ConfigMap
    `rask-console-config`, which adds `federated:id` and the audience scope every governed door requires), and calls
    the catalog and lineage APIs with its bearer, same-origin. Its bell is a browser-side log of the reader's own
    writes (`#lib/activity.svelte.ts`: each `command`'s success or failure, in sessionStorage under the principal, at
    most 100 and none older than 24 h, cleared at sign-out). `serve.ts` is its static server: the build with an
    SPA fallback, `/config.json` from `$CONSOLE_CONFIG_FILE`, and the strict CSP SvelteKit computes, sent as a header.
    Every page of the former `home` and `lakehouse` zones lives in one route tree (`/`, `/projects`, `/catalog/...`,
    `/admin/...`, `/settings/...`, `/workbench`); an admin page's guard is a
    client-side `+layout.ts`, which discloses nothing the catalog's per-request check would not refuse. See
    `.claude/skills/rask-frontend`.
  - `frontend/packages/ui` — Svelte 5 + Bits UI + Tailwind 4 design system (`@rask/ui`) with Storybook 10. The only
    frontend package with a build step (`svelte-package` → `dist/`); styled components live here, and the console's
    `app.css` reads them with an `@source` two `../` up. See `.claude/skills/rask-styling`.
  - `frontend/packages/api` — `@rask/api`, the typed `/api/*` client the console calls through, with the bearer supplied
    by a provider the app installs, the lineage client and the OpenAPI types generated from `docs/*-openapi.json`.
  - `frontend/packages/dockview` — `@rask/dockview`, the workbench's dock; `frontend/packages/flow` — `@rask/flow`, the
    graph views the governance pages draw with.
- `tests/` — `tests/unit`, `tests/integration` and `tests/e2e-py` (the live suites) for the lakehouse; the console's
  browser suite is its own Playwright suite (`frontend/console/e2e`).
- `scripts/` — **all** dev/ops scripts, shell + Python. **No production-state-changing CLIs** — data enters, and moves
  between tiers, only through the catalog's namespace doors.
- `deploy/` — the overlays (`values-*.yaml`), the alert rules, kind and k3s configs, and `deploy/fixtures/<name>`: the
  platform's development stand-ins (cloudnative-pg, dapr, dex, external-secrets, marquez, nats, observability, openbao,
  rustfs). The Dex fixture declares the console's sign-in client under its own `console` value.

**Workspace membership is globbed, per plane** — the directories are language-pure, so every child carries the right
manifest:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["console", "packages/*"]` (relative to `frontend/`)

A new Python library or service is picked up by the glob — but it **must** ship a `pyproject.toml`, or uv refuses the
workspace; a new JS package under `frontend/packages/` must ship a `package.json`, or bun silently skips it.

Deployables are workspace members with a dockerfile: `.docker/rest-catalog.dockerfile` installs and serves the
lakehouse's `catalog`, `lineage` and `maintenance` and no client (its import gate refuses `lancedb`),
and `.docker/frontend.dockerfile` the console's image (`web-console`, the static build and `serve.ts`); `.docker/cnpg-age-ext.dockerfile` and `.docker/marquez.dockerfile` build development
fixtures' images.

## Architecture

**`rask` IS AN AGNOSTIC, MULTIMODAL LAKEHOUSE. Any pipeline, on any compute and orchestrator, reads and
writes its governed tables through the catalog.** That is the whole of what it is. A Lance lakehouse under Lance
Namespace (catalog + provenance), events on Dapr + NATS JetStream, with lineage and authorization across all of it;
the compute engine and the workflow engine are the user's (ADR 0154, ADR 0155).
Any modality — text, image, audio, video, embeddings, or one nobody has written yet — is a first-class
citizen: bytes in, a governed table out, lineage emitted. The workloads that write it (audio, image and text alike)
are clients in repositories of their own; no single one of them is the platform, and if any file here reads as though
one is, that file is wrong.

**It is NOT an HTR system, and it belongs to no institution.** Never describe it as either. A data type
must never enter a shared seam — the catalog, tier governance, maintenance, lineage, the events — and the test for every one of those is *would this be right for audio?* If an answer
needs a modality's name, it belongs in the workload that writes it, outside this repository. An organisation name is
only ever an external identifier someone else owns — a model repo, a source URL, the GitHub org — never
a description of what rask is.

Key facts that aren't obvious from any single file:

- **LANCE ONLY, ALWAYS — a permanent ruling (owner, 2026-08-15), not a current-scope note.** The catalog
  stores Lance tables and **no other format, ever**. A create naming a non-Lance format is refused 400
  at the door (`core/formats.py::reject_unsupported_properties`; pinned by
  `services/catalog/tests/test_create_validates_shape_before_round_trips.py::test_a_non_lance_format_is_refused_without_dialling_out`) —
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
  such a set (`lakehouse.features.describe_foreign_data_file_versions`), and the maintenance
  counter `compaction.datasets.mixed_file_versions` pages on one. pylance 13's `update()`,
  `merge_insert().data_storage_version()` and `compact_files()` also take a `data_storage_version` that
  can stamp the flag: a stock client's such commit is refused at `version/create`, and rask code never passes it.
- **A workload is the user's, and the platform knows nothing about it.** Whatever a workload's stage graph, model or output format is, it reaches the platform as a client of the catalog's doors: bytes in, a governed table out, lineage emitted.
- **The estate is AUTHENTICATED, and this bullet used to say the opposite.** It read *"No auth, no app
  middleware. The services assume localhost / trusted network"* — which was true once and has been
  false for a while: `chart/values.yaml` defaults auth ON, every service mixes in `GovernedAuthSettings`,
  and `chart/templates/route.yaml` publishes every `/api/*` prefix at the edge. The stale sentence was not
  harmless: read as policy, "trusted network" made an unguarded service look deliberate rather than
  unfinished. Owner ruling 2026-08-26: authenticated is the truth, the ungated services get the door
  their siblings share, and body caps + rate limiting land as one seam rather than per route (the catalog's
  `lakehouse.body_limit` cap and its write-concurrency shed). That work landed: the tracking
  file, `open_fastapi-audit.md`, was **DRAINED AND DELETED 2026-08-28** — all 55 findings fixed
  RED-first, every closure adversarially re-verified (the drain's own commits carry the record).
  **rask runs no edge process** (ADR 0165): the platform's
  Gateway (Kubernetes Gateway API) serves the chart's HTTPRoutes (`chart/templates/route.yaml`), which route each
  public `/api/*` prefix straight to its service's app port, rewriting the prefix with `URLRewrite`
  `ReplacePrefixMatch` where the service mounts elsewhere (table: `rask.apiRoutes` in `chart/templates/_helpers.tpl`),
  and each service authenticates every request itself. The chart's HTTPRoute `rask-console`
  (`chart/templates/console.yaml`) sends `/` to the console's web pod on the same Gateway, and the `/api/*` and `/dex`
  routes are longer prefixes that keep winning, so the browser reaches every API same-origin with its bearer and no
  CORS. On k3s the Gateway
  is Traefik's Gateway provider (`deploy/k3s/traefik-gateway.yaml`, Gateway `traefik-gateway` in `kube-system`).
- **State surface:** the lakehouse (Lance datasets on S3; RustFS in development, `rask-rustfs`, ADR 0168) governed by the catalog, plus the chart-managed **OpenFGA** Postgres database — served by the `rask-age` **StatefulSet**, NOT by CloudNativePG. Lineage keeps no database (ADR 0158): its commit feed (`provenance$events`), table-level edge list (`provenance$edges`), outbox refusals and forwarder cursors are Lance tables in the catalog's provenance namespace, written and read through the catalog's doors, and the lineage graph is bring-your-own (the relay forwards every recorded event as OpenLineage to `services.lineage.forward.url`; Marquez in development). Measured live 2026-09-07: **zero** `cluster.postgresql.cnpg.io` objects exist, and `age.cnpgCluster.enabled` defaults **false** because the AGE-on-CNPG cutover needs a built extension image (`.docker/cnpg-age-ext.dockerfile`), K8s 1.33+ and CNPG >= 1.27 — the two paths are mutually exclusive and `age-cluster.yaml` fails the render if both are on. The CNPG **operator** is a prerequisite (ADR 0153; in development the `cloudnative-pg` fixture in `cnpg-system`) and runs with nothing to reconcile, so a running operator is not evidence that a `Cluster` exists. **The app's own relational DB is gone** (P7a): no batches table, no Alembic, no `DATABASE_URL` in the fleet. **No Redis**; events ride Dapr pub/sub on NATS JetStream. (`.docker/` still carries 3 `docker-compose*.yml` files: the local stack and two auth/dex variants. They are side stacks for local bring-up, not the deploy path; the Helm chart is.) The Helm chart in `chart/` is the single deploy artifact for both local k3s and production, and installs no operator: the External Secrets Operator, the Dapr control plane, NATS JetStream and the CloudNativePG operator are prerequisites, installed in development from `deploy/fixtures`, each its own release; the chart renders a CNPG `Cluster` only with `age.cnpgCluster.enabled`, for a cluster that serves `postgresql.cnpg.io/v1`. No compute engine is in the chart (ADR 0153, ADR 0155): the engine is bring-your-own, and the product chart names no part of it — no address, token, scrape job, alert or network peer (ADR 0189). `make k3s-up` installs the lakehouse with its console and no client (XC-183, ADR 0193). Local deploy: `make k3s-install` (one-time), then `make k3s-up`, which pulls from `K3S_REGISTRY` (default `localhost:5000`) at the tags `deploy/values-live-pins.yaml` pins, or `make k3s-build` → `make k3s-import` → `make k3s-up K3S_REGISTRY=` for side-loaded images; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Observability (optional, `observability.enabled`):** rask ships ONE thing: the **OTel Collector** (`chart/templates/otel-collector.yaml`, app-id `rask-otel`), its telemetry seam. **Vector was RETIRED 2026-07-27** (owner ruling): the Collector is the single log shipper — its filelog receiver tails infra-pod logs, and it scrapes the Dapr sidecars and control plane and OpenFGA. Every plane (the fleet, Dapr, OpenFGA) exports plain OTLP/HTTP to the Collector and names no store; the Collector exports to **`observability.export.endpoint`** with the backend's headers from `observability.export.*` (none by default; the dev overlay names GreptimeDB's db-name, trace-pipeline and audit-table headers) and basic auth from the store's `otel-collector-export` secret. **The store, dashboards and alerting are the platform's, not the chart's** ([[XC-127]], ADR 0153): the alert rules are an artefact of their own, `deploy/alerting/rules.yml` (promtool-proven by `make alert-rules-check`, replayed against GreptimeDB by `make alert-rules-drill`), for the platform's evaluator. Development gets the chain from the `observability` fixture (`deploy/fixtures/observability`, namespace `observability`): GreptimeDB (`greptimedb.observability:4000`, local volume, retention Job) → Perses (rask's dashboards) and vmalert → Alertmanager, which `deploy/values-local.yaml` names. Standard OTLP throughout (not OTel-Arrow).
- **The lakehouse runs no ingest, no stage, no training and no engine (ADR 0154, ADR 0155).** Data enters through the catalog's namespace doors and the catalog announces each committed write; nothing inside the lakehouse reacts to that announcement by copying rows into the next tier — a user's pipeline does, on its own compute. Ingest of an external source is a bring-your-own client job that commits through the catalog, built in the client's own repository; data reaches bronze only from a client speaking the namespace doors; the live lane `tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` is such a client, seeding bronze with stock pylance on the credential the catalog vends. A create that commits no rows is a DDL event, and one that commits rows is announced as a write (LH-492). **No tier carries a workload-shaped schema**: a governed row needs no column, because its run and inputs ride the commit as `rask.*` transaction properties (ADR 0156 C9, LH-225); `source_rowid` is the one column the platform names (`lakehouse.stage_stamp`), an opt-in held to uint64 stable row ids and reserved by name in the catalog (`catalog/core/provenance_guard.py`), and the rest of the row, `stage` and `lineage` included, is the writer's. A workload's own output shape belongs to the user's pipeline.
- **Source bytes are a WORKLOAD concern.** A workload may read from S3, an HTTP endpoint, a read-through cache, anywhere — the platform takes data only through the catalog's namespace doors and does not care where the bytes came from. A workload's source and sink adapters are its own code; the platform ships none and has no privileged ingress for any particular protocol.

## Conventions

- **The console calls `/api/*` same-origin, in development too.** `vite dev` proxies `/api` to `RASK_EDGE_URL` (default `http://localhost:8080`, an edge: `make dev-k3s` port-forwards k3s Traefik's Service there); the built app is served by `serve.ts` behind the platform's Gateway. See `.claude/skills/rask-services-fleet` and `rask-frontend`.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are globbed (`testpaths` names `packages/*/tests`, `services/*/tests` and the three root suites), not discovered from the rootdir.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **oxfmt uses tabs**, single quotes, `printWidth: 100` — defined in `frontend/.oxfmtrc.json`, applied across the console and every package under `frontend/packages`. Prettier is gone.
- **The JS workspace runs plain bun scripts** (`frontend/package.json`): `build` builds `@rask/ui` then the console, and `check`/`lint`/`fmt`/`fmt:check`/`test` run each package's own script through `bun run --filter '*'`. Add a new JS package's scripts in its own `package.json`; only `knip` stays root-level, because it analyses the whole JS graph at once.
- **The console is a client-rendered single-page app, Svelte 5 strict.** Every `.svelte` change is validated with the Svelte 5 skills + the `svelte` MCP autofixer. Its CSP is strict (no `unsafe-eval`): a dependency that builds code at runtime breaks under it, which is why `frontend/patches/` carries an apache-arrow patch.
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
- **The code names no reference product** (owner, 2026-11-07: "I don't like you mentioning lakekeeper in code"). A
  comment, docstring, test, chart value or script states the rationale itself; the comparison with Lakekeeper (or any
  catalog the research reads) stays in questions, ADRs, `docs/`, issue bodies and commit messages.
  `scripts/code_names_no_reference_product.py` enforces it in `make lint` and `dagger call lint`.

## Agent skills

### Issue tracker

GitHub Issues on `AI-Riksarkivet/rask`, one issue per row (`LH-243: …`), ordered by the pinned `Phase 1 map` issue. See `docs/agents/issue-tracker.md`.

### Workflow (the mattpocock skills, plugin `mattpocock-skills`)

Work moves idea → ship along one path, and `/ask-matt` explains any step. Every step holds its output to the four
facts in § Engineering principles, GitOps first (GitOps, cloud native, Lance-namespace/table-idiomatic, zero trust):

1. `/grill-with-docs` sharpens an idea by interview and records terms in `CONTEXT.md` and decisions as ADRs.
2. `/to-spec` writes the spec as an issue; `/to-tickets` splits it into rows with native blocking edges.
3. `/implement <#row>` builds one row: `/tdd` red-green slices at the seams, the type checker and single test
   files as it goes, the full suite once, then `/code-review` (Standards and Spec reviewers in parallel; one
   round, every finding in the row's own code fixed), then a commit. `/clear` between rows.
4. `/triage` admits rows nobody on the team wrote; `/diagnosing-bugs` takes a hard bug to a red test first.

**Every row, whoever works it, follows the collaboration rules in `docs/agents/issue-tracker.md`:**

- **Claim first.** `gh issue edit <n> --add-assignee @me` is the first write, before a branch or a test. An
  assigned row is its assignee's; take the next one from the map's frontier (first unassigned, unblocked row).
- **Coherent rows ship together, straight to `main`** (owner, 2026-10-14: one person works the repo, and a CI round
  costs 30-40 min for nothing the local gates miss; owner, 2026-11-07: "bunching tickets that are coherent or similar
  and just doing them", since a commit, build, deploy and read-back per small row is time spent on ceremony). Open rows
  that touch the same seam or the same deploy go into one change: one review round, one commit on `main` that names
  each row with `Refs`, the local gates once (the full offline suite, `ruff`, `ty`, `make lint-imports`, the comment
  gate, `make openapi` when a route changed), one push, one build, one deploy, and a read-back that proves each row's
  closes-when. No PR. A real defect found while working is fixed in that change when it is in or next to the work,
  case by case, rather than written down for later (owner, 2026-11-07: "why not just fix them").
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
| The cluster route, an endpoint's public prefix, a 404/401/403 through `/api/*`, ports | `rask-services-fleet` |
| A catalog endpoint, the spec's error codes, the hierarchy guards, trash/protection, maintenance | `rask-lance-catalog` |
| The console: a route, sign-in, data fetching, the CSP, the frontend gates and e2e | `rask-frontend` |
| `@rask/ui`, tokens, `class=`, an unstyled page, a new component | `rask-styling` |
| Whether a feature's outcome can reach a PERSON — emitting a run/control event a notifier targets by, someone who should have been told and was not, a new `ControlAction` | `rask-notifications` |
| A Dapr component vs hand-rolling — queuing, scheduling, locking, config, secrets, middleware; which blocks this estate uses/refused and why | `rask-dapr` |
| An authorization model, tuples, `.fga` files | `openfga` |
| RustFS: the dev/CI fixture, its security, admin API, S3 behaviour, operations | `rask-rustfs` |
| Writing, changing, deleting or auditing any test; a test audit or prune | `testing-python` + `rask-testing` |
| Anything under `chart/`, a subchart or CRD, `helm upgrade`, a value that never reaches a pod, the 1 MiB release ceiling | `rask-helm` |

These skills are maintained against the code and **will drift** — when you find a claim that contradicts a file, fix the skill in the same commit as the code.
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
