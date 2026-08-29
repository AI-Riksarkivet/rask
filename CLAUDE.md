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
  that proves the defect, then the fix — not ad-hoc edits. (This used to name the "superpowers flow";
  that plugin is not installed, so the instruction pointed at nothing. `docs/superpowers/` is dated
  residue from when it was — do not treat those plans or specs as current.)
- **No silent scope-cuts.** If you bound coverage, sample, or defer something, say so
  explicitly. Don't let "partially done" read as "done".

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
- **Python uses uv** (3.13) with Ruff + `ty` for type-checking. Run Python via `uv run <cmd>`; type-check via `uvx ty check`.
- Identifiers and env vars carry **no `ra-`/`ra_` prefix** (legacy from the ra-batch migration). Env vars are `RASK_*`.

## Common commands

| Goal                         | Command                                                                  |
| ---------------------------- | ------------------------------------------------------------------------ |
| First-time setup             | `make install` (= `bun --cwd=frontend install` + `uv sync --all-packages`)              |
| Build everything             | `make build`                                                             |
| Run tests (excludes slow)    | `make test` (= `uv run pytest -m "not slow"`)                            |
| Run all tests incl. slow     | `make test-slow` (needs real models / a GPU)                             |
| Single Python test           | `uv run pytest services/core/tests/test_pipelines.py::test_name`        |
| Filter by name               | `uv run pytest -k <pattern>`                                             |
| Skip slow tests              | `uv run pytest -m "not slow"`                                            |
| Format + lint + typecheck    | `make check` (= `make fmt` + `make lint` + `make typecheck` + `make knip`) |
| Scan dependencies for CVEs   | `make audit` (osv-scanner over all six lockfiles + `.dagger/go.mod`)     |
| Scan dockerfiles + chart     | `make scan-config` (trivy misconfig + secret detection)                  |
| Scan git history for secrets | `make scan-secrets` (trufflehog; gates on VERIFIED credentials only)     |
| Scan a built image           | `make scan-image NAME=gateway` / `make scan-zone-image ZONE=home`        |
| Frontend type-check only     | `bun --cwd=frontend run check` (one zone: `bunx turbo --cwd=frontend run check --filter=home`) |
| Storybook for `@rask/ui`     | `make storybook` (→ `:6006`)                                             |
| Bootstrap Claude Code config | `make claude-bootstrap`                                                  |

### Run the app locally

```bash
make ray-up            # local Ray head on :6379, dashboard :8265
make serve-up          # deploy the active runner's model services on Ray Serve
make dev-micro         # the fleet: gateway :8888 + compute :8804 + controlplane :8820 +
                       #   the explorer viewer :8101 (via scripts/dev-micro.sh)
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
the bronze→silver→gold cascade, lineage emission, FGA checks. For those, build and deploy:

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
stores left are the chart-managed lineage (AGE) and OpenFGA databases.)

## Repository layout

Two **language-pure planes** — **don't blur them**. Python lives at the repo root (`packages/` + `services/`); the entire JS/TS estate lives under `frontend/`, its own bun + Turborepo workspace root. (There is deliberately **no Polylith-style `projects/` layer** — it was removed 2026-07; deployables build straight from the root uv workspace via `uv sync --package <name>`, one dockerfile per deployable in `.docker/`.)

- `packages/` — reusable **Python** libraries, **no entrypoints**. uv workspace members.
  - `packages/storage` — the S3 CLIENT seam: `s3_client`/`S3Client`, `configured_endpoint`, `derive_hcp_creds`, `split_s3_uri`/`merge_prefix`, and the `StorageError`/`BucketNotFoundError`/`ObjectNotFoundError` + `s3_errors` family. It still exports `FSSource/Sink`, `S3Source/Sink`, `iter_keys` and `build_source`/`build_sink`, but **those are no longer the fleet's source/sink seam** — this line read as if they were. The lakehouse plane's adapters live in `service_kit.lakehouse.sources` / `.sinks` (what `ingest.adapters` registers and what the medallion's media produce uses); `build_source`/`build_sink` and the FS pair have their only production caller in `runners/htr`. **Protocol-agnostic:** a source only every modality can use belongs here; one only a single workload uses belongs in that runner (an IIIF read-through cache lived here until 2026-08-17 with exactly one consumer, and moved to `runners/htr`)
  - `packages/service-kit` — shared **platform library**: `make_service_app` app factory, `Settings`/config, exceptions, middleware, `get_settings`/`SettingsDep`, the injectable lifespan. Dependency-light (no lancedb/ray/sqlmodel).
  - `packages/ray-kit` — Ray Job SDK + dashboard wrapper (schemas, `build_client`, `RAY_TRANSIENT_ERRORS`, the dashboard service). Used by the `compute` service.
  - `packages/tracker` — pluggable transfer-state tracking (SQLite / Postgres backends)
  - `packages/validate` — pre-upload image validation (TIFF/JPEG/PNG corruption detection + pluggable rules)
  - `packages/lineage-kit` — the OpenLineage emission kernel used by the medallion producer/movers
  - `packages/ray-cluster-env` — a deps-only member (`package = false`, ships no code) NAMING the Ray images' platform environment; both Ray dockerfiles `uv sync --package ray-cluster-env` from the root lock. (`packages/ratch` was DISSOLVED 2026-08-28 — owner ruling, `open_ray-kernel.md`: its Ray-interaction half was a drifted third submission seam retired for `ray-kit`, its schema/dataset layer was vendored into the sealed audio runners that consumed it undeclared, and its CLI had no callers. With it went the one sanctioned CLI exception — packages ship no entrypoints again, without exception.)
- `services/` — runnable **Python** code. **The old monolithic `viewer` service is gone**, and so is the whole batches/orchestrator plane (P7a compute-plane cutover — see `docs/architecture/lance-ns-merge.md` P7):
  - `services/gateway` — reverse proxy on `:8888` (the frontend's proxy target). Path-routes `/api/*` longest-prefix-first: the `compute` rows (`/api/ray`, `/api/serve` — the URL namespace names the Ray cluster, not the service), `/api/projects` → controlplane, plus the lance-plane rows (`/api/catalog`, `/api/lineage`, `/api/produce`, `/api/train`, `/api/explorer/*`); owns no state; **no `/api` catch-all** — unmatched `/api/*` 404s. Upstreams are env-overridable (`RASK_COMPUTE_URL` :8804, `RASK_CONTROLPLANE_URL` :8820, `RASK_EXPLORER_VIEWER_URL` :8101, …).
  - `services/compute` — the **`compute` service** (`:8804`): Ray dashboard introspection (`/api/ray/*`) + the `/api/serve/*` proxy (thin shell over `ray-kit`); no DB. `compute` on every surface — uv member, import, k8s/dapr/image/gateway (R22, supersedes R20's `ray` + its ray-api PyPI-shadow exception); the public paths stay `/api/ray` + `/api/serve`. (`core`/`core_api`/`search_api`/`volumes_api` died in the R6/R20 media wave — the S3 object browser now lives in the lance `viewer` at `/api/explorer/object*`; lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/explorer/search`.)
  - `services/notifications` — the **targeted inbox** (`:8850`, app-id `notifications`) behind the estate's bell. One Dapr `InboxActor` per subject holding claim-check POINTERS with durable read state, so the badge counts YOUR work rather than the estate's. Two ingresses: a `lineage.events.v1` subscription and a `bindings.cron` reconciler over lineage's durable `GET /events` (the bus alone is provably incomplete — ingest, Ray TRAIN and external OpenLineage producers emit over HTTP only). SIX targeting sources: authorship, ORIGINATOR (a service-authored run done FOR a named person), `project#member`-gated project watches, the grant pair, and the task pair (assigned/unassigned/changes-requested/dropped). Delivery gates on `can_be_notified`, render on `can_get_metadata` — see `.claude/skills/openfga` and `rask-services-fleet`. **COVERAGE IS DECIDED AT THE PRODUCER, not here:** a state change that names nobody is not under-delivered, it is undeliverable, and `notifiable()` answers an event it cannot target with a SUCCESS ack — so a producer that stamps a role literal in `author.sub`, omits `lance.project`, or emits no event at all fails silently and is reported by nothing. Before adding a feature whose outcome a person should hear about, read `.claude/skills/rask-notifications` (the contract).
  - `services/medallion` — the lakehouse cascade (producer + movers). R23: raw is the external world, never a governed tier — the medallion is exactly bronze→silver→gold. The producer exposes **exactly three write doors, all root-mounted (NOT under `RASK_API_PREFIX`) and all token-guarded**: `POST /produce` seeds the bronze dataset and emits the ONE bronze-write OpenLineage event — this is the **cascade head**, and dropping that emit means the whole bronze→silver→gold run silently never happens; `POST /ingest-media` lands external media as bronze blobs and drives the media chain; `POST /train` submits training. `/bronze-arrival` subscribes to the bronze-write event and publishes the `medallion.bronze` trigger, so the cascade is driven by the ARRIVAL event rather than by the ingest call. (Those three are the whole INGEST surface — there is no protocol-specific ingest door, and adding one would make that protocol privileged. They are not the whole ROUTER surface, which this line used to claim: `producer.py:180-196` mounts SIX routers — health, produce, ingest-media, train, plus `promotions` (the quality gate's third answer: a mover that HOLDS a promotion publishes it here and a `can_promote` holder answers on `/promotions/*`, gateway-routed as `/api/promotions`) and `mover_ops` (the cascade's operator door at `/movers/*`, gateway-routed as `/api/movers`, which authorizes and forwards workflow terminate to the mover that hosts the instance) — and registers the `/bronze-arrival` and promotion Dapr routes on top. Both extra routers are human-facing control, not ingest, which is why they do not weaken the three-doors rule.)
- `frontend/` — the **JS/TS plane** and its own workspace root: `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/` (the shared favicon source). The only JS outside it is `tests/e2e`, a standalone Playwright project with its own lockfile (`make e2e`).
  - **The 7 zones** (`frontend/microfrontends/<zone>`), SvelteKit 2 + Svelte 5, **SSR** via `svelte-adapter-bun` (a real Bun server: `bun ./build/index.js`), composed by Turborepo's built-in microfrontends proxy on `:3024` in dev and the k3s Ingress in prod: `home` (catch-all, base `''`, owns `/` + the OIDC BFF), `lakehouse` (`/lakehouse` — data/lineage/models/admin/storage areas), `explorer` (`/explorer`, labelled **Explorer**), `annotator` (`/annotator`, labelled **Annotate**), `compute` (`/compute`), `models` (`/models`, the model plane — it REPLACED `train`, on train's port 5178; `train` has zero tracked files and a leftover `microfrontends/train/` or `microfrontends/media/` on a dev host is untracked build residue, not a zone), `studio` (`/studio`). Bases are a bare `/<zone>` — **there is no `/default/` segment**. Every zone renders the shared `@rask/ui/shell` AppShell. See `.claude/skills/rask-frontend`.
  - `frontend/packages/ui` — Svelte 5 + Bits UI + Tailwind 4 design system (`@rask/ui`) w/ Storybook 10 (`@storybook/svelte-vite`). The only frontend package with a build step (`svelte-package` → `dist/`). **Styled components live here, not in the zones** (zones supply `app.css` with an `@source '../../../packages/ui/dist'` — three `../`). 40 subpath exports incl. **`@rask/ui/shell`**. See `.claude/skills/rask-styling`.
  - `frontend/packages/api` — `@rask/api`, the shared data layer: typed gateway client **plus** the OIDC/BFF plane (`bff.ts`, `oidc.ts`) and the lineage client. valibot. JIT TS, no build step.
  - `frontend/packages/explorer-api` — `@rask/explorer-api`, the Arrow-backed explorer/viewer client.
  - `frontend/packages/engine` — `@rask/engine`, a framework-agnostic PixiJS/WebGPU annotation canvas (ra-anno lineage).
  - `frontend/packages/labeling` — `@rask/labeling`, the `LabelOp` model + the annotator's Arrow-IPC transport.
  - `frontend/packages/config` — `@rask/config`, one shared `tsconfig.base.json` (extended by 6 of 14 packages).
  - `frontend/packages/zone-contract` — `@rask/zone-contract`, **imported by nothing at runtime**: 21 files gating the estate's shape (cross-zone `data-sveltekit-reload`, the zone manifest, deploy paths, and a toolchain guard that fails the build if ESLint/Prettier reappear), **plus the dev scripts those gates guard** — `src/dev-zone.ts` (`make dev-zone`) and `src/proxy.ts`. Dev tooling lives *inside* a package on purpose: `lint`/`fmt` are per-package turbo tasks, so a script parked outside both workspace globs would be linted and formatted by nothing.
- `scripts/` — **all** dev/ops scripts, shell + Python: one-shot setup / debug tools (`harvest_ead`, `index_catalog`, `download_*`, `smoke_s3`, the self-contained Ray jobs `ray_stage_job`/`ray_train_job`, …) plus `dev-micro.sh` and `k3s-install.sh`. **No production-state-changing CLIs** — ingestion and the cascade run through the HTTP services (the medallion producer + movers).

- `runners/` — **sealed model environments, NOT workspace members. This is the ONLY place a modality may live.** One directory per workload, each holding its Ray Data pipeline (`src/runner`) *and* its model actors in one project with its **own `pyproject.toml` and own `uv.lock`**. Matched by no glob, which is the point: a runner's heavy ML stack (torch, ultralytics, transformers, …) never enters the fleet's resolution (root lock 200 → 145 packages; fleet tests ~32 min → ~6 s). `storage` is a **path** dep. A runner's tests are invisible to the root pytest — `make test` runs them separately; its images build from **its** lock; it carries its own ruff config (ruff resolves the nearest pyproject). Ray entrypoint: `uv run --project runners/<workload> runner`, overridden in-cluster by `RASK_RUNNER_CMD=runner`. A second workload is a second sibling directory, and needs no change anywhere else. **A workload's awkward dependencies are ITS problem — never solved by fattening a shared image.** **RESOLVED 2026-08-25: PER-WORKLOAD BAKED IMAGES** (owner decision). `.docker/ray-cluster.dockerfile` is the agnostic head image — it builds `packages/ray-cluster-env` (the deps-only platform-env member) from the ROOT lock and installs no runner at all (verified in the built image: torch, htrflow, ultralytics and cv2 absent). One workload's image is built from the PARAMETRIZED `.docker/ray-runner.dockerfile` via `ARG RUNNER`, exactly as `frontend.dockerfile` builds all seven zones from `ARG APP`: `scripts/dagger-image.sh --runner <runner> --tag ray-<runner>:<tag>`. A Serve application declares its own image as `ray.serveApplications[].image`, rendered into `runtime_env.image_uri`, so several workloads share one cluster without sharing one environment. A tenth runner is a new `--runner=` value and edits no file. Note the distinction, because the word is overloaded: the 2026-08-23 ruling rejects shipping DEPENDENCIES through `runtime_env` (its `pip`/`uv` fields, resolving a stack at replica start), and that still stands. `runtime_env.env_vars` (scoping ENVIRONMENT to one application or job) and `runtime_env.image_uri` (naming a prebuilt image) are both fine and are how this is wired. **The two environments do not merge, and that is the seal working:** a runner image's default interpreter is the workload's venv, so `import lance` fails there by design — the platform's Lance plane belongs to the head image, which runs the jobs that need it. Pinned by `tests/unit/test_invariants.py::test_the_deployed_ray_image_PROVIDES_every_serve_application_it_declares`, which refuses a declared application whose import path no image it can run provides.

**Workspace membership is globbed, per plane** — the directories are language-pure, so every child carries the right manifest:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["microfrontends/*", "packages/*"]` (relative to `frontend/`)

A new Python library/service or a new zone is picked up by the glob — but it **must** ship a `pyproject.toml` (Python) or a `package.json` (JS), or its plane silently drops it.

Deployables are just workspace members with a dockerfile: `.docker/<name>.dockerfile` runs `uv sync --frozen --package <name>` against the **root** `uv.lock` (the deployable set is `gateway`, `compute`, `notifications`, `runner` — the `.docker/compute.dockerfile` image is the compute service; the Ray cluster image is `.docker/ray-cluster.dockerfile`).

## Architecture

**`rask` IS AN AGNOSTIC, MULTIMODAL LAKEHOUSE. It runs batch processing on ANY workflow, and lets you
ANNOTATE and SEARCH the data.** That is the whole of what it is. A Lance lakehouse under Lance Namespace
(catalog + OpenLineage into AGE), durable execution and events on Dapr + NATS JetStream, batch compute
on Ray, annotation and search over the governed tables, with lineage and authorization across all of it.
Any modality — text, image, audio, video, embeddings, or one nobody has written yet — is a first-class
citizen: bytes in, a governed table out, lineage emitted. That is observable, not aspirational: there are
**nine sealed runners** (`asr`, `assist`, `diarize`, `dummy`, `htr`, `insid3`, `kg`, `topics`,
`voiceprint`), spanning audio, image and text. No single one of them is the platform, and if any file
here reads as though one is, that file is wrong.

**It is NOT an HTR system, and it belongs to no institution.** Never describe it as either. A data type
must never enter a shared seam — the catalog, the cascade, the sweep, annotation, search, lineage,
notifications — and the test for every one of those is *would this be right for audio?* If an answer
needs a modality's name, it belongs in a sealed `runners/<workload>`, whose internals (stage graph,
models, GPU packing, output format) this file deliberately does not describe. An organisation name is
only ever an external identifier someone else owns — a model repo, a source URL, the GitHub org — never
a description of what rask is.

See `docs/architecture/system-overview.md` for the full diagrams. Key facts that aren't obvious from any
single file:

- **LANCE ONLY, ALWAYS — a permanent ruling (owner, 2026-08-15), not a current-scope note.** The catalog
  stores Lance tables and **no other format, ever**. A create naming a non-Lance format is refused 400
  at the door (`data.py::_reject_unsupported_format`; pinned by `tests/unit/test_format_guard.py`) —
  that 400 is the final answer, not a gap awaiting Parquet/Iceberg/Delta support. This is load-bearing,
  not a preference: the catalog is deliberately format-AWARE (imports pylance, serves the data plane
  in-process, coordinates commits), and all three are only sound because the format is closed. It is
  also why the estate needs no relational DB — Iceberg puts the commit pointer IN the catalog so every
  commit is a DB transaction, while Lance puts the CAS in the object store. A second format would
  reintroduce exactly the requirement this architecture is built to avoid. Anyone needing Iceberg
  interop should use Polaris or Lakekeeper; it is a road not taken, not a backlog item.

- **A workload is a SEALED RUNNER, and the platform knows nothing about it.** `runners/<workload>` submits one Ray Data pipeline per CLI invocation and blocks on `.materialize()`; long-lived model weights stay warm behind **Ray Serve** deployments the runner owns and `make serve-up` deploys independently of any job. Serve replica counts and GPU fractions are the RUNNER's business (`RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC` plus literals inside its own pipeline module) — never the platform's. Whatever a workload's stage graph, model or output format is, it reaches the platform as config: bytes in, a governed table out, lineage emitted. Its per-workload mechanics live in that runner's own README, not here.
- **The estate is AUTHENTICATED, and this bullet used to say the opposite.** It read *"No auth, no app
  middleware. The services assume localhost / trusted network"* — which was true once and has been
  false for a while: `chart/values.yaml` defaults auth ON, nine services mix in `GovernedAuthSettings`,
  and `chart/templates/ingress.yaml:66` publishes `/api` at the edge. The stale sentence was not
  harmless. It is the reason `compute` and `controlplane` still ship with **no authn/authz code path at
  all** — no `GovernedAuthSettings`, no `security.py` — while the gateway carries both to the public
  edge (`{prefix}/ray`, `{prefix}/projects`, `/api/serve`); read as policy, "trusted network" makes an
  unguarded service look deliberate rather than unfinished. Owner ruling 2026-08-26: authenticated is
  the truth, the ungated services get the door their siblings share, and body caps + rate limiting land
  as one seam in `service_kit.middleware.register_middleware` rather than per route. That work landed:
  the tracking file, `open_fastapi-audit.md`, was **DRAINED AND DELETED 2026-08-28** — all 55 findings
  fixed RED-first, every closure adversarially re-verified (the drain's own commits carry the record). The frontend hits `/api/*` on the **gateway** (`:8888`), which path-routes to the per-domain services; `/api/ray/*` and the `/api/serve/*` proxy are served by the standalone **ray** service (over `ray-kit`). SSR `load`/remote functions reach the gateway server-side via an absolute base URL (`RASK_GATEWAY_URL`); client code uses the relative `/api/*` proxy. The gateway sits **behind** the SvelteKit Bun server (it does not serve the SPA shell).
- **State surface:** the lakehouse (Lance datasets on RustFS S3) governed by the catalog, plus the chart-managed **lineage (AGE)** and **OpenFGA** Postgres databases (CloudNativePG). **The app's own relational DB is gone** (P7a): no batches table, no Alembic, no `DATABASE_URL` in the fleet. **No Redis**; events ride Dapr pub/sub on NATS JetStream. (`.docker/` still carries 8 `docker-compose*.yml` files — auth/dex, governance, lineage, rustfs, demo, local. They are side stacks for local bring-up, not the deploy path; the Helm chart is.) The Helm chart in `chart/` is the single deploy artifact for both local k3s and production — in-cluster CloudNativePG (`Cluster`), RustFS operator (`Tenant` → `rask-rustfs-io:9000`), and KubeRay are gated by `cnpg.enabled`/`rustfs.enabled`/`ray.enabled` values toggles; each toggle gates both the operator subchart and its custom resource. Local deploy: `make k3s-install` (one-time) → `make k3s-build` → `make k3s-import` → `make k3s-up`; tear down with `make k3s-down` / `make k3s-purge`. See `docs/architecture/deployment.md` and `chart/README.md`.
- **Observability (optional, `observability.enabled`):** **OTel Collector** (`chart/templates/otel-collector.yaml`, app-id `rask-otel`) → GreptimeDB (on RustFS S3 bucket `rask-observability`) → Perses. **Vector was RETIRED 2026-07-27** (owner ruling, recorded in `chart/Chart.yaml`): the Collector is the single log shipper — its filelog receiver tails infra-pod logs into `opentelemetry_logs`, and it also scrapes the Dapr sidecars. Alerting is a SEPARATE chain and none of the above can do it: **vmalert** evaluates `chart/alerting/rules.yml` against GreptimeDB (`-datasource.url`) and hands firing alerts to **Alertmanager** (`-notifier.url`), which dedupes/routes to Slack/PagerDuty (`chart/templates/alerting.yaml`). The Collector transports, GreptimeDB stores and answers PromQL, vmalert is the only thing that turns a series into a page. Fleet (incl. the gateway) + Ray export OTLP/HTTP **traces and RED metrics** to `rask-greptimedb-standalone:4000/v1/otlp` via `service_kit.setup_otel` (FastAPI/HTTPX instrumentation emits `http.server.*` metrics automatically). OTLP headers split by signal: traces carry `x-greptime-pipeline-name=greptime_trace_v1` (GreptimeDB requires it for trace ingestion → `opentelemetry_traces` table), metrics use db-name only (→ PromQL series). The chart provisions a Perses "Fleet — RED" dashboard (`chart/templates/perses-dashboards.yaml`). Standard OTLP throughout (not OTel-Arrow).
- **The orchestrator is gone (P7a).** Ingestion is the medallion producer's `POST /produce` (bronze dataset + the ONE bronze-write OpenLineage event through `packages/lineage-kit`) or `POST /ingest-media` for external media. Neither publishes `medallion.bronze` directly — the `/bronze-arrival` subscription reacts to the write event and fires the cascade, which is why a dropped emit silently cancels the entire run. The governed tiers are exactly bronze→silver→gold. A workload runs as event-triggered movers on the unified Ray cluster, and **no tier carries a workload-shaped schema**: every governed row is `{id, payload, stage, lineage, source_rowid}` with `payload` OPAQUE — the transform declares its shape, the tier does not (`medallion/schemas/tier.py`). A per-workload gold contract did live there (`schemas/htr.py`, nine of eleven columns describing transcribed page images) and was **deleted 2026-08-17** for making the cascade a transcription pipeline wearing a lakehouse's name; `medallion/schemas/` now holds only `tier.py` and `events.py`. A workload's own output shape belongs to its sealed runner. **The Ray lane submits `python /home/ray/jobs/<job>.py`** — those scripts are baked by `.docker/ray-cluster.dockerfile`, the image the chart's KubeRay cluster runs; a job whose entrypoint the image lacks dies `exit 2` and the stage reports FAILED with nothing naming the image.
- **Source bytes are a WORKLOAD concern.** A runner may read from S3, an HTTP endpoint, a read-through cache, anywhere — the platform ingests through `POST /produce` / `POST /ingest-media` and does not care where the bytes came from. `packages/storage` supplies the source/sink adapters; the platform has no privileged ingress for any particular protocol.
- **Remote KubeRay:** the runner accepts `--address ray://...:10001`. No K8s manifests live in this repo — the remote cluster is managed elsewhere.

## Conventions

- **Gateway port is 8888 — but the zones' dev proxies disagree on who to call.** `compute`/`studio`/`models` proxy `/api` → `VIEWER_BACKEND` (`:8888`, the gateway); `home`/`lakehouse` proxy → `LANCE_BACKEND` (**`:8001`**, the lineage service — which `dev-micro.sh` does not start); `explorer`/`annotator` have no `/api` proxy and reach `:8101`/`:8102`/`:8103` via their own BFF. The same split exists server-side: `compute` reads `RASK_GATEWAY_URL`, `home`/`lakehouse` read `LANCE_GATEWAY_URL`. A `/api/*` call that works in one zone can fail in another — see `.claude/skills/rask-frontend`.
- **Pytest import mode is `importlib`** (`--import-mode=importlib` in `pyproject.toml`). Test paths are explicit (`testpaths = [...]`), not discovered.
- **Ruff line length is 160**, not 100. Selected rule families include `ANN` (annotations); tests are exempted via `per-file-ignores`.
- **oxfmt uses tabs**, single quotes, `printWidth: 100` — defined in `frontend/.oxfmtrc.json`, applied across every JS/TS workspace (zones, `@rask/ui`, `@rask/api`, `@rask/zone-contract`). Prettier is gone.
- **JS monorepo runs on Turborepo** (`frontend/turbo.json`): `bun --cwd=frontend run build`/`check`/`dev` delegate to `turbo run` (package tasks + `^build` ordering + cached `build`/`.svelte-kit`/`dist` outputs). Add a new JS package's scripts in its own `package.json` — never centralize task logic in root. `lint`/`fmt`/`fmt:check` are **per-package turbo tasks** too (each package runs `oxlint` / `oxfmt`); only `knip` stays root-level, because it analyses the whole JS graph at once.
- **The cross-zone link gate is a test, not a lint rule.** A cross-zone `<a>` must carry `data-sveltekit-reload` or SvelteKit soft-navigates into a route the zone doesn't own (→ 404). Enforced by `@rask/zone-contract`'s vitest suite (`frontend/packages/zone-contract/src/cross-zone-reload.test.ts`) — oxlint's `.svelte` support reads the `<script>` block, not the markup, so an anchor-attribute rule cannot live there.
- **Frontend is SSR + Svelte 5 strict.** Every `.svelte` change is validated with the Svelte 5 skills + the `svelte` MCP autofixer. Browser-only globals must stay inside `onMount`/`$effect`/handlers (never component top level or `load`) or SSR render crashes.
- **`ty` is configured with `error-on-warning = true`** — typecheck warnings fail CI.

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

These skills are maintained against the code and **will drift** — when you find a claim that contradicts a file, fix the skill in the same commit as the code.
- See `.claude/README.md` for the full plugin/marketplace/MCP surface and bootstrap steps.
