<!-- Status: PLAN — decisions marked ACCEPTED below are owner-ratified; the rest stay PROPOSED.
     Authored 2026-07-24. RE-PINNED 2026-07-27: main@df70b63 -> main@502150b (190 commits), with owner
     ruling R8 and the structural drift folded in — see "Re-pin 2026-07-27" below.
     Source repo: /home/blackwell/Desktop/lance-ns
     (re-pin to current lance-ns main at each phase copy — copies are taken fresh, never stale). -->

# Merge plan: lance-ns → rask (`feat/lance-ns-merge`)

Source of truth for the copy is `/home/blackwell/Desktop/lance-ns` at **`502150b`** (re-pinned 2026-07-27 from `df70b63`); target is `/home/blackwell/Desktop/rask` on `feat/lance-ns-merge` (carries the `projects/`-layer removal, `06a60a4`). The vendored reference copy at `/home/blackwell/Desktop/lance-ns/rask/` is NOT a merge source or target.

**Amendment (2026-07-24, re-pin `c2ae04f` → `df70b63`).** The 14 commits between the pins are (a) the **media plane** — the lance-audio fold: `services/{viewer,search,annotator}` + `media`/`annotator` frontend zones + `media.yaml`/corpus mount + catalog-mode bearer identity — and (b) **OIDC hardening** — Dex served on the app origin (`/dex`), split-horizon issuer discovery, a login-first gate. Owner rulings folded into this revision:

1. **Total merge** — *everything* in lance-ns moves into rask, media plane included (ACCEPTED).
2. **Compute-plane convergence** (was out of scope) — the event-driven medallion REPLACES rask's S3-sync orchestration entirely; rask's HTR pipeline becomes the lance-ray-seam jobs the cascade triggers; batch IO is Lance-only. New phase **P7 — Convergence** below (ACCEPTED as direction; sequencing per decision 6).
3. **Serialization is a separate microservice** — compute ends at gold Lance; a new `exporter` service projects consumer formats (ALTO 4.4 first) from gold. Never inside the lakehouse or the movers (ACCEPTED). See P7c.

---

## Re-pin 2026-07-27 — `df70b63` → `502150b` (190 commits)

The plan's own rule is to re-pin at each phase copy. This is that re-pin. Three of the drifts are
**structural**, not cosmetic: anyone executing P2 against the old table goes looking for four zones that no
longer exist. Full working detail in the source repo at `docs/MERGE-REPIN-DELTA.md`.

| The plan expected | Reality at `502150b` | Rows fixed below |
|---|---|---|
| 7 lance zones (`data`, `lineage`, `models`, `admin`, `media`, `annotator`, `home`); "the 6 lance zones"; turbo build across "13 zones" | **Four**: `home`, `lakehouse`, `media`, `annotator`. `bb099df` merged data+lineage+models+admin into **one `lakehouse` zone** — its four areas are routes, not apps | P0 table · P2.4 · P4 · P6 |
| `frontend/packages/api` (`@rask/api` fork), `frontend/packages/rask-ui` (`@rask/ui` fork) | **Seven `@repo/*` packages**: `api`, `ui`, `config`, `engine`, `labeling`, `media-api`, `zone-contract`. The last two are net-new | P0 table · P2.2–2.3 |
| `frontend/eslint-rules/cross-zone-reload.js` → a local eslint plugin | **The directory does not exist.** The frontend is on **oxlint + oxfmt** (`.oxlintrc.json`, `.oxfmtrc.json`, `frontend/TOOLING.md`); there is no prettier. The cross-zone-reload guard survives as a *test* in `@repo/zone-contract` | P0 table · P2.1 · P2 gates |
| — | **Dapr state store** `lance-statestore` (`state.postgresql`, `actorStateStore: "true"`, DSN from OpenBao) | P0 table · §1 externalization · P4 |
| — | **Run-notification transport** — every zone's shell holds a `query.live` SSE stream open | P4 (ingress) · P6 |
| — | `runners/assist` + `chart/templates/runners.yaml` + `.docker/assist-runner.dockerfile`; catalog `user_state`/`me`/`access_admin` endpoints | P0 table · P1 gateway rows · P3 |

**Easier than the plan assumed:** four zones instead of seven; AGE-on-CNPG **decided and proven** (stock
image + ImageVolume, `docs/CNPG-AGE.md`) so PROPOSED decision 1 is settled; tenancy is **no longer a
decision** (per-warehouse physical multi-tenancy shipped, rask's single implicit `default` is the degenerate
case); catalog 501s **confirmed at 7** (`docs/COVERAGE.md`, 47/54 backed).

## P0 — Branch hygiene + repo-layout decision (first commit is a doc, not code)

**Hygiene rules (whole branch):**
- All work commits land on `feat/lance-ns-merge` in `/home/blackwell/Desktop/rask`. Never push to any rask remote. Never commit to or merge with rask `main`. Never edit `/home/blackwell/Desktop/lance-ns` (copy out only).
- Conventional commits (rask `cliff.toml`); each phase = one reviewable commit series; each commit message cites the lance-ns source commit (`c2ae04f`) — full git-history grafting (subtree/filter-repo) is explicitly out of scope, provenance is by citation.
- First commit: `docs/architecture/lance-ns-merge.md` — the layout table below, the naming rules, and the five decisions restated with status PROPOSED. rask's `docs/architecture/` is its living design record; this is where the plan lives, not in scratch files.

**Layout decision (where everything lands):**

| lance-ns source | rask destination | Rule |
|---|---|---|
| `services/{catalog,lineage,medallion,compaction,viewer,search,annotator}` | `components/services/{catalog,lineage,medallion,compaction,viewer,search,annotator}` (converted to src-layout: `src/<name>/…`, entrypoints preserved: `catalog.main:app`) | rask shape: workspace member + `.docker/<name>.dockerfile` per deployable (no `projects/` layer — removed 2026-07). The media trio (viewer/search/annotator, folded from lance-audio) is IN scope (total merge, owner-ruled); lance `search` coexists with rask's `search_api` until P7 retires the latter (gated on the P5 pin test) |
| `services/common` | `packages/common` (distribution name `lance-common`, import root stays `common` so zero import rewrites) | Transitional; long-term converge on `service-kit`'s `make_service_app`, keeping common's auth/FGA/audit middleware as the governed variant — NOT on this branch |
| ~~`frontend/components/frontends/{data,lineage,models,admin}`~~ **`frontend/components/frontends/lakehouse`** | `components/frontends/lakehouse` | **RE-PIN**: the four are one zone since `bb099df` — `data`/`lineage`/`models`/`admin` are its *routes*. One app, one port slot, one ingress rule, one `.docker` build, one spec dir. Per **R8** it also absorbs rask's `storage` (the S3 object browser) and `train` |
| `frontend/components/frontends/{media,annotator}` | `components/frontends/{media,annotator}` | The media zones are **`ssr=false` SPAs with NO BFF** — root-absolute `/api/*` fetches, a different deploy/env shape than the SSR zones; their fetch bases are rewritten to the `/api/media/*` namespace (owner-ruled, see P1) |
| `frontend/components/frontends/home` | **dissolves into rask's `components/frontends/home`** (`/auth/{login,callback,logout}` routes + zone-picker landing content move in; lance home package deleted) | One catch-all only |
| `frontend/packages/api` (**`@repo/api`**) | merged into `packages/api` (add `oidc.ts`, `bff.ts`, `gateway.test.ts`, **`runs-feed.ts`**, export conditions `./oidc`,`./bff`,`./runs-feed`) | rask original wins. **RE-PIN**: the scope is `@repo/*`, not `@rask/*`. `runs-feed.ts` is the shared run-notification generator every zone's bell stands on |
| **`frontend/packages/{config,engine,labeling,media-api,zone-contract}`** (net-new) | `packages/*` | **RE-PIN — no plan row existed.** `zone-contract` is the gate package (bff-routes, budget, cross-zone-reload, link-targets, live-stream, manifest, notification-surface, no-networkidle, poll-reason, proxy) and is what makes the frontend claims falsifiable; `media-api` is the media seam incl. the shared server cache |
| `frontend/packages/ui` (**`@repo/ui`**) | merged into `packages/ui` (add the components + `gsap.ts`/`motion.ts` + shell fold; keep rask's storybook) | rask original wins; see P2. **RE-PIN**: the shell now also carries `notification-center`/`notification-list` and the `runs/run-status` ordering — the bell is shell furniture, not a zone feature |
| ~~`frontend/eslint-rules/cross-zone-reload.js`~~ | — | **RE-PIN: the directory no longer exists.** lance-ns is on **oxlint + oxfmt**, no prettier and no eslint. The guard survives as a TEST in `@repo/zone-contract`, so it needs no rask lint plugin — but P2's gate list must stop saying "eslint (incl. cross-zone-reload rule)" |
| `chart/templates/*` (see P4) | grafted into `rask/chart/` | One umbrella, `fullnameOverride: rask` stays |
| `.docker/{rest-catalog,ray-lance,cnpg-age-ext,assist-runner}.dockerfile` (**`assist-runner` is new**) | `rask/.docker/` (same names; paths rewritten to new layout) | frontend.dockerfile: rask's parametrized one wins, adopt lance-ns's `oven/bun:1.3.14-slim` pin; images become `<app>:tag` (drop `lance-` prefix) |
| `scripts/*.sh`, `scripts/*.py` | `rask/scripts/` (no name collisions with `dev-micro.sh`/`k3s-install.sh`/`vendor-rustfs-operator.sh`) | Object names/ports inside adapted in P6 |
| `tests/unit`, `tests/integration` | `rask/tests/unit`, `rask/tests/integration` | Appended to root `testpaths` (rask uses explicit testpaths — unlisted dirs silently never run) |
| `tests/e2e` (Python live suites) | `rask/tests/e2e-py` | Avoids colliding with `tests/e2e` = the `@rask/e2e` bun package (decision 4 target) |
| `docs/{DATA-CONTRACT,CNPG-AGE,RAY,RASK-INTEGRATION}.md`, `docs/{catalog,lineage}-openapi.json` | `rask/docs/` + zensical nav entries | RASK-INTEGRATION.md becomes the merge record's appendix |
| `deploy/cnpg-age-cluster.yaml` | `chart/templates/age-cluster.yaml` | Decision 1 |
| `deploy/ray-lance-demo.yaml` | **replaced** by a `RayCluster` CR template (P5) | Option B |
| **`chart/templates/dapr-statestore.yaml`** (net-new) | `chart/templates/` | **RE-PIN — no plan row existed.** `state.postgresql` with `actorStateStore: "true"`, DSN resolved from OpenBao through `lance-secrets` (never a k8s Secret). Externalizes to CNPG like the rest. Its `scopes` must list every app that owns operational state — an app outside `scopes` gets "component not found" from its sidecar and every user's saved work 503s, which the sidecar logs and nothing else notices |
| **`chart/templates/runners.yaml`** + `runners/assist` (net-new) | `chart/templates/` + `components/runners/assist` | **RE-PIN — no plan row existed.** A deployable absent from the P3 image list |

**Naming rules (the load-bearing ones):**
1. **k8s objects**: backends `rask-<service>` (existing pattern); ALL frontend zones — rask's included — become `rask-web-<zone>`. Rationale: lance-ns's `lineage` zone vs `lineage` backend Service under one release is the exact selector-collision class lance-ns already hit (memory landmine + `frontends.yaml` NAMING comment). Renaming rask's 7 zone objects is branch-only churn and buys a uniform rule.
2. **Dapr app-ids**: backends keep bare names (`catalog`, `lineage`, `medallion`, `compaction`, `core-api`, `gateway`); frontends get no sidecars. No collisions in that set today; the rule prevents future ones.
3. **Ports (dev)**: rask's `microfrontends.json` slots win; lance zones get fresh slots (data 5180, admin 5181, models 5182, lineage 5183, media 5184, annotator 5185); `:3024` proxy and `:5273` home are rask's. `PORT_OFFSET` in `dev-micro.sh` is the escape hatch for backend clashes.
4. **Gateway**: rask's FastAPI gateway (:8888, Dapr-aware) wins; lance-ns's nginx gateway retires (P1/P4).

---

## P1 — Python plane (hermetic; no cluster)

**Pre-step (before or parallel to P1) — Ray version unification (owner-ruled: ONE cluster, latest version).** Provable against rask alone, no lance-ns code involved: bump rask's Ray estate to the latest release (runner `pyproject` lifts `ray<2.56`; `ray-kit` re-tested; `.docker/{ray,runner}.dockerfile` bases rebuilt; `chart/templates/rayservice.yaml` image), align the Python minor with what lance-ray/pylance-8 support, and **revalidate the GPU Serve packing invariants** (fractions × replicas ≤ physical GPUs, host-RAM headroom, `smoke-gpu.sh`, `/transcribe` + `/htrflow` answer). Gate: rask's existing HTR smoke path green on the new version. Everything after lands on the unified cluster.

**Moves/adaptations:**
- Copy the seven services + common per the P0 table; convert to src-layout; add workspace members to root `pyproject.toml` (`packages/common` + the 7 services) and regenerate `uv.lock`.
- Drop lance-ns's `pythonpath=["services"]` hack — src-layout members resolve via workspace installation under rask's `importlib` import-mode.
- Reformat all incoming Python under rask's ruff config (line 160) in a **separate pure-format commit**; fix whole-repo `ty` (error-on-warning) fallout.
- Append `tests/unit`, `tests/integration`, `tests/e2e-py` to rask's explicit `testpaths`.
- **Gateway fold (code half)**: add rows to `components/services/gateway` `_routes()` — `/api/catalog→catalog`, `/api/lineage→lineage`, `/api/produce`+`/api/train→medallion`, and the **whole-plane media namespace** (owner-ruled): `/api/media→viewer`, `/api/media/search→search`, `/api/media/annotations→annotator` (all three SPAs' fetch bases rewritten accordingly — bare `/api→viewer` cannot coexist with rask's `/api→core-api` catch-all). Port the `lance.lineageSidecarOnlyRoutes` nginx 403-blocklist as gateway middleware (it was helm-template logic; it becomes Python). Respect the `dev-micro.sh` warning: lance services serve `/v1/...` internally, gateway strips `/api/<svc>` — a wrong prefix silently 404s.
- **Catalog naming — resolved by absorption, no rename** (owner-ruled 2026-07-24, superseding the earlier rename ruling): lance `catalog` is THE catalog. rask's EAD/discover surface (`/api/v1/catalog`, `core/services/discover/`, the discover zone) is legacy placeholder-quality and is **eaten by the media plane at P7** — no rename is spent on it; during P1–P6 the prefixes already differ (`/api/catalog` vs `/api/v1/catalog`), so coexistence needs zero code change.
- FGA model triplet (`common/auth/model.fga`, `model.json`, `model.fga.yaml`) moves as one piece with `test_fga_model_contract.py`.

**PRECONDITION — rask's own `ty` gate is RED today (measured 2026-07-27).** `uvx ty check` on the
UNMODIFIED `feat/lance-ns-merge` tree reports **70 errors**, concentrated in
`components/scripts/index_alto.py` (39), `components/services/core` (24), `packages/htr/src` (10),
`components/services/ray_api` (7), `packages/storage/src` (4). None of them is lance-ns code. The plan's
own gate below requires `make check` green, and P1 says "fix whole-repo `ty` fallout" as if the only
fallout were the incoming code — it is not. Either clear rask's 70 first (a standalone commit, provable
without any lance-ns code) or the gate is unachievable and will be quietly downgraded mid-phase, which is
the failure mode this plan exists to prevent. The pre-commit hook enforces it, so this also blocks
committing anything on the branch: the plan's own re-pin commit needed `--no-verify`.

**Gates green after**: `uv sync` clean; `make check` (ruff format --check + ty); `make test` with a **collection-count assertion** (re-derived at copy time — ≥67 unit + 13 integration files at `df70b63`; the guard against silent testpaths loss); rask's Dagger `TestPg` unchanged and green; `test_invariants.py` + `test_fga_model_contract.py` green (topic constants, outbox-only publish, FGA relations vs compiled model).

**Live-proof**: `uv run uvicorn catalog.main:app` boots, `/health` 200, and an FGA-off request fail-closes (503/401) — proving the fail-closed posture survived the move.

## P2 — Frontend workspace unification

Ordered sub-steps (the 3-way merge with a lost base only works in this order):
1. **Normalization commit** — **RE-PIN: the premise changed.** lance-ns has no prettier; it formats with
   `oxfmt` + `rsvelte-fmt` and lints with `oxlint`, and `@repo/zone-contract` asserts **byte-identical**
   `lint`/`fmt`/`fmt:check` scripts in every workspace package (proven to fail on drift in both directions).
   So the choice is explicit rather than incidental: either rask adopts the oxlint/oxfmt toolchain with the
   incoming code, or the incoming code is reformatted to rask's prettier and the zone-contract script-parity
   test is retargeted. **Decide before any fold** — whichever way, do it as one pure-format commit first, so
   the ~30-file cosmetic diff collapses to the semantic one before the 3-way merge.
2. **`packages/api` merge**: copy in `oidc.ts` (sealed AES-256-GCM cookie), `bff.ts` (`makeSessionHandle`/`sessionToUser`), `gateway.test.ts`; add export conditions. Keep rask's `AGPL-3.0-only` label (see risk 8).
3. **`packages/ui` merge**: add `chip`, `search-bar`, `select`, `status-board`, `lib/gsap.ts`, `lib/motion.ts` (`{@attach}` factories). Shell fold — take from lance-ns: `authEnabled`/`user: null` signed-out state in `app-shell.svelte`/`nav-user.svelte`, trailing-slash `norm`/`exact` matchers, `shell/breadcrumb.ts`; keep from rask: `navMain(project)` factory, path-derived project (`segs[0]`), storybook + `css.d.ts`. lance-ns's host-based project derivation (`projectFromHost`) is **not** adopted (see risk 3 / not-do list).
4. **Zones** — **RE-PIN: three, not six.** Move `lakehouse`, `media`, `annotator` in (lance `home`
   dissolves into rask's home as before). The media pair are `ssr=false` SPAs with no BFF, fetch bases
   rewritten to `/api/media/*` per P1. Register in root `package.json` + merged `microfrontends.json`; the
   old six-slot port allocation collapses to three (`lakehouse`, `media`, `annotator`). Zones keep flat
   bases (`paths.base: '/lakehouse'` …) alongside rask's `/default/<zone>` — coexisting URL families,
   transitional. Per **R8**, rask's `storage` and `train` zones are retired *into* `lakehouse` rather than
   carried, and `discover` retires at P7.
5. **Home merge**: rask home absorbs `/auth/{login,callback,logout}` and the zone-picker; add a **reserved-segment guard** to `routes/[project]/+page.server.ts` rejecting `data|lineage|models|admin|media|annotator|auth|api|default|capi` as project ids (kills the `/data` → `/data/overview` → 404 trap).
6. rask zones stay auth-free: `authEnabled: false` when OIDC env is absent (`makeOidcConfig(env)` already tolerates this); no retrofit of remote-function data layers. **OIDC deltas since the original pin** land with the fold: split-horizon issuer discovery (public issuer string vs in-cluster fetch — two env keys in the shared `frontendEnv`) and the **login-first gate** (signed-out page loads redirect to `/auth/login`) — the gate is wired **only when `frontend.oidc.enabled`**, otherwise rask zones' auth-free posture would break.

**Gates** — **RE-PIN**: one bun lockfile; `turbo build` across the merged zone set (**recount at copy time**
— 3 incoming + rask's surviving set under R8, not "13"); `build-storybook`; svelte-check; **the lint/format
gate per the step-1 decision** (not "eslint incl. cross-zone-reload" — that guard is a zone-contract test
now); knip; `fmt:check`; bun tests; **the whole `@repo/zone-contract` suite**, which is what makes the
frontend claims falsifiable and must be wired into rask's test run or it silently never executes.
**Live-proof**: dev proxy `:3024` up — rask home hydrates, `/lakehouse` hydrates, `/media` hydrates,
`/annotator` hydrates, `/auth/login` resolves; `/compute` unaffected.

## P3 — Images + Dagger

- `rest-catalog.dockerfile` rewritten to build from `components/services/*` + `packages/common` via `uv sync --package <project>` (rask's pattern) — **and it must cover (or split images for) the media trio**: at `df70b63` viewer/search/annotator share the catalog image (`services/` COPY'd whole), which the src-layout conversion breaks; `ray-lance.dockerfile` re-based onto the unified Ray version (P1 pre-step) and `cnpg-age-ext.dockerfile` copied as-is; unified `frontend.dockerfile` per P0 builds all 13 zones.
- Merge lance-ns's `.dagger` Go functions (`Test`, `Lint`, `Typecheck`, `Openapi`, `Charts`, `Frontend`, `TestLineage`) into rask's `.dagger` module — one `dagger.json`, `TestPg`/`MigrateUp` untouched, source paths rewritten.

**Gates**: `dagger call` on every function green locally; hadolint. **Live-proof**: all images build and `kind load` / `k3s import` succeed.

## P4 — Chart unification

- **Subchart dedupe** (one control plane each): keep rask's deps for nats 2.14.2 / dapr 1.18.1 / openfga 0.3.9 / cloudnative-pg 0.28.3 / kuberay / rustfs-operator; lance-ns **values** win where richer — nats (credentialed, netpol'd), openfga (`datastore.engine: postgres` + migrate hook **replaces** rask's memory toggle, weight −5). CNPG CRDs stay vendored in `chart/crds/` with `crds.create=false` — lance-ns must not re-install them.
- **Graft templates**: `services.yaml` (+lineage cron binding), `medallion.yaml` (producer + 4 movers + per-mover DLQ), `compaction.yaml`, **`media.yaml`** (the viewer/search/annotator Deployments + the `media-catalog-token` bearer Secret; the `/var/media-corpus` node-local hostPath is kind-only — resolve in-phase to a PVC or a rustfs-backed corpus bucket before k3s/prod, no hostPath ships), **`dex.yaml` at its `df70b63` shape** (served on the app origin at `/dex` + restart-on-config-change); **delete** lance-ns `gateway.yaml` (nginx) — rask's gateway carries the routes from P1 incl. the `/api/media/*` rows; `frontends.yaml` merged with the universal `web-` prefix + `frontend-session` Secret (media zones as static SPA deploys, not Bun SSR); `ingress.yaml` = rask's template with all 6 lance zones appended to `frontend.apps` (template needs zero changes — the socket already exists).
- **Hooks** (all Job/CronJob pod templates carry explicit component labels — the netpol landmine): `nats-stream-job` (LINEAGE + DLQ `dlq.>` streams; Dapr jetstream does not auto-create), `openfga-migrate`, `bootstrap-admin` (weight 5, seeds `owner` on `warehouse:lance_catalog`), `greptimedb-ttl-job`, rustfs bucket-init (re-pointed at operator Tenant endpoint `<tenant>-io:9000`), openbao seed, `backup-pg` + `backup-snapshot`.
- **NetworkPolicies**: port ALL of them (recount at copy time — "13" predates the media plane) AND extend to rask's fleet — default-deny now covers rask pods, so new allows: ingress→`web-*` frontends, gateway→fleet ports 8801–8810, core-api/orchestrator→`rask-postgres-rw:5432`, ray-api→dashboard 8265, search-api→rustfs. Enumerate before applying; kindnet hides violations.
- **Dapr resources**: `dapr-component.yaml` (lance-pubsub, catalog-control-pubsub, per-app subscriber pubsubs with the two retry profiles, `lance-secrets` OpenBao store), `dapr-app-token.yaml`, `dapr-resiliency.yaml`.
- **AGE**: `age-cluster.yaml` (CNPG Cluster + ImageVolume extension image) replaces `age-postgres.yaml` (decision 1).
- **RustFS**: rask's operator Tenant wins; lance-ns's per-warehouse bucket provisioning code (`warehouse_registry.py` and bucket-init) re-pointed at `<tenant>-io:9000`; keep-PVC posture via Tenant spec.
- **Net-new**: greptimedb-standalone + perses subcharts, otel-collector, openbao, dex, alerting (+ `rules_test.yml`), perses-dashboards, external-secrets, security-sa, ha.yaml, merged `values-prod.yaml`; `prod_render_check.sh` adapted to `rask-` names.

- **RE-PIN — live streams need the ingress to permit them, and rask's is Traefik.** Every zone's shell now
  holds a `query.live` SSE stream open for the run-notification bell. Proven on lance-ns at **270.1s with 2
  streams and 0 severed**, clearing both nginx's 60s default and Bun's 255s `IDLE_TIMEOUT`. Two things carry
  it and **only one travels**: the application keepalive in `@repo/api/runs-feed` (re-yields the last pulse
  every 20s — ours, moves with the code), and `nginx.ingress.kubernetes.io/proxy-read-timeout: 3600` on the
  Ingress (**not ours to keep** — Traefik needs its equivalent). Without it every zone reconnects on a timer
  and each reconnect re-primes the event window and writes an audit record. Verify with
  `scripts/verify_live_stream_timeout.mjs`, which takes `HOLD_S` — run it past 255 against rask's ingress.
- **RE-PIN — the media corpus hostPath.** Already ruled "no hostPath ships"; the work that satisfies it is
  lance-ns `#103` (corpus as catalog-governed project tables). It is deferred on the lance-ns side and
  **blocking here**.

**Gates**: helm lint; `make charts` render invariants (incl. uniqueness assertion on rendered object names — the collision guard); `prod_render_check` (0 plaintext secrets, HA/deny flags on); promtool alert-rules proof; `test_invariants.py` chart checks (no dead env vars, helm-set keys exist). **Live-proof**: fresh kind install — all pods Ready, all hooks Completed, 13+ NetworkPolicies present, JetStream streams exist (nats CLI), OpenFGA model migrated + bootstrap tuple readable, AGE Cypher round-trip, rask's own fleet (gateway/core-api/ray-api/search-api) still serves under default-deny.

## P5 — Ray plane (owner-ruled: ONE cluster, latest version — Option B revoked)

- The P1 pre-step already unified rask's Ray estate on the latest release. This phase folds the lance jobs onto it: the `ray-lance` image content (pylance 8.x + lance-ray pins + jobs baked at `/home/ray/jobs/`) merges into the unified ray image (or a job-runtime-env), and `deploy/ray-lance-demo.yaml` is retired. GPU (Serve TrOCR/htrflow) and CPU (lance movers) workloads share the one cluster — Kueue admission keeps the lanes from contending.
- `medallion.ray.address` values → the unified cluster head; `ray_submit.py` unchanged (version-agnostic Jobs-REST seam, idempotent reattach, TRACEPARENT injection).
- **The load-bearing pin test first**: bump rask dev-group `lancedb`/`pylance` from floating `>=0.20` to explicit lance-ns-era pins; add an integration test that writes a DSV 2.2 + stable-row-id dataset with pylance 8 and opens/FTS-indexes/queries it with rask's lancedb. If the bump breaks `search_api`/`discover`, the search-reuse seam is gated and documented — do not proceed on assumption.
- Creds: replace rask's `("AWS_","HCP_",…)` prefix-glob runtime_env passthrough with lance-ns's explicit env-var list style in `core/services/submission.py` (prefix-glob leaks any future secret into the Ray dashboard).

**Gates**: ray-kit tests, medallion unit tests, `e2e-ray-ci` as a **dedicated** job (movers/OpenBao secret-race landmine — never inside e2e-stack). **Live-proof**: `MEDALLION_RAY_ENABLED=true` mover cascade submits to the KubeRay head, `ray_stage_job` completes, OpenLineage RunEvent with DatasetVersion facet lands in AGE; resubmit proves idempotent reattach; rask's `/htrflow` Serve route still answers.

## P6 — e2e extension + CI vehicle + the global live drive

- **RE-PIN — `waitUntil: 'networkidle'` can never fire again in any zone.** The shells hold a live stream
  open by design, so an idle-network wait sits until its own timeout and then reports the product as
  hanging. lance-ns hit this on ten waits; `@repo/zone-contract/no-networkidle.test.ts` now fails on a new
  one. New spec files must wait on the ELEMENT they act on, or assert the effect and retry
  (`expect(async () => {…}).toPass()`).
- Extend `@rask/e2e` (`tests/e2e`) per decision 4: new spec files for `/lakehouse|/media|/annotator` hydration (**RE-PIN**: the four lance areas are routes of one zone now), gateway round-trips through `:8888` (incl. the `/api/media/*` rows), the **login-first-gate redirect flow** + split-horizon issuer verification, the media seeder's bearer-mode live suites, auth-redirect Location hygiene; `RASK_E2E_BASE_URL` mechanism unchanged. rask's `mfe.spec.ts` untouched.
- Python live suites at `tests/e2e-py` + Makefile: import lance-ns's kind lifecycle targets (`bootstrap/kind-up/deps/images/load/deploy/up`, `e2e-ci`, `e2e-ray-ci`) **alongside** rask's k3s targets — kind is the proof vehicle on this branch (that's where the stack is proven); k3s reconciliation is deferred and documented.
- CI: the merged Dagger functions are the execution vehicle (rask's GH CI is docs-only). Draft the workflow mapping (test / frontend / lineage-e2e / e2e-stack / ray-e2e / auth-e2e → Dagger calls) as a file on the branch, but the branch is never pushed — so the enforced proof is local `make ci` + `make e2e-ci` + `make e2e-ray-ci` runs, logged in the merge doc.
- Adapt verify scripts (`verify_produce_door.sh`, `verify_cross_zone_oidc.sh`, `verify_merge_lineage.sh`, `governance_e2e.sh`, `e2e_stack.sh`) to `rask-`/`rask-web-` object names and rask gateway `:8888` prefixes.

**Global live-proof (the phase gate)** on the merged chart on kind: `seed_medallion_fga.sh` + restart lance-ray (drive-readiness landmine: green e2e ≠ drive-ready) → alice `/produce` 202 / bob 403 / anon 403 → cascade rows per stage in the tenant bucket → lineage graph populated → cross-zone OIDC (sign in on `/data`, still signed-in on `/admin`; alice 2xx / bob 403) → rask `mfe.spec` green against the **same** deploy (home + all `/default/*` hydrate) → DLQ view + replay → `prod_render_check` green.

## P7 — Convergence (the compute-plane cutover; owner-ruled IN scope, sequenced AFTER P6 is green)

The coexistence merge (P1–P6) keeps rask's orchestrator running untouched. P7 executes the target architecture on the proven base:

**a. IIIF → bronze ingest producer.** No S3-ObjectCreated exists for IIIF, so ingestion is a producer job honoring the lance-ray seam contract (`RASK-INTEGRATION.md`): harvest IIIF pages → write the bronze **blob-v2 page-image Lance dataset** (the `ray_stage_job.py` media path is the precedent) → emit ONE raw-write OpenLineage RunEvent — and **never publish `medallion.raw` itself** (the `/raw-arrival` subscription fires the cascade; publishing both double-fires it). This replaces the prefetch lane + `IIIFCachedSource`'s cache role.

**b. HTR stages as movers, ending at gold.** Re-cut the runner pipeline as event-triggered movers on the unified cluster: bronze page-images → silver (Layout/Lines regions+geometry) → gold (transcriptions). Only the two IO endcaps change — `PageLoaderActor`/`AltoWriterActor` are replaced by Lance reads/writes; Layout/Lines/TranscribeViaServe (still calling the warm Serve handle) transfer as-is. Each mover: read upstream Lance version-range → transform → write downstream → emit the `DERIVED_FROM` edge + version facet → publish the next trigger; FGA `can_create_table`/`can_promote` gates; vended short-TTL table creds via the catalog (workload identity, no durable secret on compute). **The gold schema contract is pinned here**: page dims, region/line polygons, reading order, text, confidences — everything a serializer needs; a field dropped from gold is unrecoverable downstream.

**c. The `exporter` service (owner-ruled: serialization is a separate microservice).** New `components/services/exporter` (+ dockerfile): projects consumer formats from gold Lance — never inside the lakehouse or the movers. ALTO 4.4 first (the serializer extracted from `AltoExportActor` into a plain library the service imports); future formats (PAGE XML, plain text, IIIF annotations, hOCR) are new functions, zero pipeline changes. Two surfaces: sync single-document export (`GET …/export/{doc}?format=alto`) and async bulk export for the Archives deliverable (whole volume → a *delivery* bucket — egress artifact, outside the lakehouse; optionally emits a terminal read-lineage event so "delivered ALTO for volume X from gold vN" is provenance).

**d. Decommission (only after a+b+c are live-proven end-to-end).** Flip `RASK_ORCHESTRATOR_AUTOSTART` off fleet-wide, then delete: `core/services/orchestrator/{loop,derive}.py`, `core/services/sync.py`, the `orchestrator` entrypoint + `:8810` (dev-micro.sh row, gateway row, chart), the batches/chunks/orchestrator endpoints + the `batches` table + its Alembic lineage (formally abandoning the parked batch_state migration), the prefetch `PipelineSpec` + `PrefetchActor`, the runner's S3-diff resumability, and `components/scripts/{build_batches_db,chunk_batches,index_alto}`.

**The media plane eats rask's discovery/viewing estate wholesale** (owner-ruled — rask's surfaces there are placeholder-quality, not worth preserving): retire `search_api` (`:8802`; lance `search` over a catalog-governed lines table replaces it — gated on the P5 pin test), retire the **discover zone** + the EAD `/api/v1/catalog` endpoints + `core/services/discover/` (the EAD harvest re-lands as an ingest job writing a **catalog-governed Lance table** that the media estate serves — `harvest_ead` refitted, `index_catalog` retired), and retire `volumes_api`'s page/ALTO viewing endpoints (lance `viewer` + the exporter cover them; IIIF *reading* becomes a library inside the P7a bronze producer). What survives of volumes_api is only the `/objects` S3 browser backing the **storage** zone. Reorganize the `@rask/ui` shell nav around the merged set — **RE-PIN, now owner-ruled as R8**:
**home + lakehouse + media + annotator + compute**. `discover` retires with its backends; `storage` folds
INTO lakehouse (an S3 object browser is a lakehouse view of the warehouse's own buckets); `train` folds in
with it via lance `models`, which is a lakehouse route; `overview` folds into home. **`studio` is the one
zone no ruling covers — decide it before P2.4.**

**Gate**: the HTR-cascade e2e — IIIF → bronze → silver → gold on the unified cluster with lineage populated, then an exporter round-trip producing byte-valid ALTO 4.4 from gold — green **before** anything in (d) is deleted.

## P8 — Sweep + record

Retire `components/services/viewer` (pycache husk — distinct from lance's incoming `viewer`; resolve the directory collision at P1 copy time), root strays (`batches.db.20260527T105358Z`, `.coverage`), lance-ns nginx-gateway remnants, and any per-zone playwright configs superseded by `@rask/e2e`. Finalize `docs/architecture/lance-ns-merge.md`: decision statuses updated with evidence; git-cliff changelog preview. Update `CLAUDE.md`, `docs/architecture/*`, and the vendored `rask-*` skills (`rask-orchestrator` dies with the loop; `rask-services-fleet`/`rask-architecture` redrawn for the merged fleet). **Named follow-up, out of scope here: the platform renames to `Lagom`** (repo, chart release, docs identity) — a dedicated rename pass after the merge stabilizes. No push.

---

## Risk register (top 8)

| # | Risk | Mitigation |
|---|---|---|
| 1 | **lancedb 0.30.2 reader vs pylance 8 writer** — rask's search stack embeds a pre-8 lance core; DSV 2.2 + stable row ids + blob-v2 datasets may be unreadable, silently gating all "reuse rask FTS" value | P5 does the pin bump + a write-with-8/read-with-lancedb integration test **before** any search wiring; if red, search reuse is explicitly gated, not assumed |
| 2 | **`@rask/ui` 3-way merge with lost base** — ~30 changed files, ~8 semantic; wrong-order merging bakes formatter noise into conflicts | P2 step 1: prettier-plugin-tailwindcss normalization commit on the rask side first; then fold only the shell files + 4 new components; storybook build is the regression canary |
| 3 | **Contradictory project IA + double catch-all** — path-projects (rask) vs host-projects (lance-ns); `/data` missing its ingress rule becomes "project data" → 404 | One home (rask's) absorbs auth + landing; reserved-segment guard in `[project]` route; host-based addressing NOT adopted on this branch (parent-domain-cookie question deferred); ingress rule per zone asserted in the charts gate |
| 4 | **k8s/helm name collisions** — `rask-lineage` twice (zone vs backend), duplicate Dapr/nats/openfga/cnpg control planes, CNPG CRD re-install | Universal `rask-web-<zone>` rule; single subchart per infra with lance-ns values overlay; `crds.create=false` kept; render-time uniqueness assertion in `make charts` |
| 5 | **Silent gate loss** — rask's explicit `testpaths` means unlisted dirs never run; rask has no GH test CI, so lance-ns's guarded invariants (claim-lint, FGA model contract, prod-render) could go unenforced | testpaths appended + collection-count assertion in P1; Dagger module is the CI vehicle; local `make ci`/`e2e-ci` runs are the branch's enforcement, logged in the merge doc |
| 6 | **Ray version unification regression** (owner-ruled: ONE cluster on the latest release, in-branch — Option B revoked). The GPU Serve packing was OOM-tuned on 2.55/py3.13; a version bump can shift memory behavior and re-trip the raylet-killing cascade | The P1 pre-step does the bump FIRST, standalone, gated on rask's existing HTR smoke path (`smoke-gpu.sh`, `/transcribe` + `/htrflow` answering, fractional-sum + host-RAM invariants rechecked) before any lance code lands; the Jobs-REST seam stays version-agnostic either way |
| 7 | **Auth retrofit regressions in rask zones** — session handling leaking into 7 auth-free zones | rask zones get no hooks changes; `authEnabled:false` default when OIDC env absent; `mfe.spec.ts` green on the merged deploy is the acceptance check |
| 8 | **License contradiction** — lance-ns relabeled its `@rask/ui`(MIT-origin) and `@rask/api`(AGPL-origin) forks Apache-2.0, plus an uncommitted repo-wide AGPL→Apache swap in lance-ns; rask identity is AGPL-3.0-only | Merged packages restore rask's original labels (ui MIT, api AGPL-3.0-only, repo AGPL); incoming lance-ns Python code enters under repo AGPL; the relabel is flagged in the merge doc as a user decision — no contradictory metadata ships |

| 9 | **rask's `ty` gate is red before the merge starts** — 70 errors on the unmodified branch, so P1's `make check` gate cannot pass and the pre-commit hook blocks every commit | Clear them in a standalone pre-P1 commit, provable against rask alone; do not let the gate be relaxed to accommodate them |
| 10 | **Two incompatible frontend toolchains** — rask is eslint+prettier, lance-ns is oxlint+oxfmt with a zone-contract test asserting byte-identical scripts across every package | Decide the direction at P2 step 1 and do it as one pure-format commit; whichever way, retarget or keep `@repo/zone-contract`'s script-parity test so it still fails on drift |

(Named but below the line: netpol default-deny silently blocking hook Jobs — mitigated by the component-label rule + invariant test; `helm --reuse-values` empty-key gotcha — all new values keys use hasKey+ternary; kind same-tag image gotcha — deploy scripts delete pods after `kind load`, verify imageID digest.)

---

## Owner rulings (2026-07-24) — ACCEPTED, supersede anything above that conflicts

| # | Ruling |
|---|---|
| R1 | **Total merge** — everything in lance-ns moves into rask, media plane included. |
| R2 | **Compute-plane convergence is in scope** as P7, sequenced coexistence-first: P1–P6 land with green gates and rask's orchestrator untouched; P7 then replaces S3-sync orchestration entirely (no reconcile loop, no prefetch lane, no batches table survives). |
| R3 | **One Ray cluster on the latest version** — Option B (two clusters) revoked; unification is the P1 pre-step, proven against rask's HTR pipeline alone before any graft. |
| R4 | **Serialization is a separate microservice** (`exporter`) — compute ends at gold Lance; formats (ALTO 4.4 first) are projections served from gold, never produced inside the lakehouse or the movers. The gold schema contract (P7b) is the load-bearing artifact. |
| R5 | **Whole-plane media namespace** — `/api/media/{,search,annotations}` → viewer/search/annotator; all three SPAs' fetch bases rewritten. |
| R6 | **rask's discovery/viewing estate is eaten by the media plane** — discover zone, EAD `/api/v1/catalog`, search_api, volumes page/ALTO viewing all retire at P7 (no renames spent on them); EAD data re-lands as a catalog-governed Lance table. |
| R7 | **Platform renames to `Lagom`** — after the merge stabilizes; a named follow-up, nothing renamed on this branch. |
| R8 | **The surviving zone set is `home + lakehouse + media + annotator + compute`** (2026-07-27). Three parts: (a) rask's **browse / viewing / search** surfaces are eaten by the media plane — this is R6, reconfirmed; (b) what survives of rask's own frontend is **compute** — the Ray dashboard, jobs, actors, cluster views — because that is the plane rask owns; (c) rask's **`storage` zone folds INTO the lakehouse**: an S3 object browser is a lakehouse view of the warehouse's own buckets, not a separate destination. `train` folds in with it (lance `models` absorbed it, and `models` is a lakehouse route). `overview` folds into home as already proposed. **`studio` is undecided** and is the one zone no ruling covers — it must be decided before P2.4, not defaulted. |

**Defaults written in as PROPOSED (veto in review):** lance `models` absorbs rask `train`; `overview` folds into home; rask zones stay auth-free this branch (`authEnabled:false` unless `frontend.oidc.enabled`); relational remainder after P7 = the `openfga` + `lineage` databases only; the media corpus hostPath is replaced by a PVC or rustfs-backed bucket in P4 (no hostPath ships).

## The five PROPOSED decisions, restated with survey evidence (not relitigated)

1. **AGE on CNPG via ImageVolume** — *strengthened*: rask already ships cloudnative-pg 0.28.3 with vendored CRDs (`chart/crds/`, `crds.create=false`), so the AGE Cluster rides an existing dep with zero new operators. Caveat: the CSI-mount leg needs K8s 1.33+ — verify the kind/k3s node version in P4 before cutting over from `age-postgres.yaml`.
2. **Keycloak→FGA seam later; Dex stays** — *materially sharpened*: rask contains **zero** Keycloak, OIDC, or auth code anywhere (grep-clean); the Keycloak premise comes from the RA org environment (ra-hcp), not rask. Dex + sealed-cookie BFF is the only working auth in either repo. The seam is already env-parameterized end-to-end: `makeOidcConfig(env)` is issuer-agnostic and `frontend.oidc.publicIssuer` is the single knob; Keycloak-later = new issuer value + a subject-sync job into the same FGA tuple space + callback redirect URIs. No shell changes needed.
3. **Zone names stay as-is** — *holds*: the two zone sets are disjoint except both homes (resolved: rask home absorbs lance home's auth + landing) and the `/data`-as-project catch-all trap (resolved: reserved-segment guard). Chart-level corollary: the `web-` object prefix becomes universal.
4. **Extend rask's tests/e2e, don't replace** — *holds and is purely additive*: rask `tests/` is playwright-only; every Python gate arrives with no counterpart. New evidence: it needs an execution vehicle (rask GH CI is docs-only) → merged Dagger module + Makefile; and rask's floating `>=0.20` lance dev-specs should be pinned at lance-ns levels so rask's e2e re-resolves rather than keeping two resolutions.
5. **NATS HA / nack operator stays parked (#20)** — *holds*: rask's JetStream is on but streamless (decorative); lance-ns's stream-job + Dapr pubsub are the first real consumers, single nats subchart with lance-ns's richer values. rask's orchestrator loop is self-declared transitional toward a JetStream consumer — a real convergence hook, explicitly out of scope here.

---

## Explicitly NOT done on this branch

- No push to any rask remote; no commits to or merge with rask `main`; no edits to `/home/blackwell/Desktop/lance-ns`.
- No NATS HA / nack operator work (#20 parked).
- No Keycloak integration or Dex removal (seam documented only).
- ~~No Ray version reconciliation, no touching rask's orchestrator loop or head-local htr jobs~~ — **REVOKED by owner rulings R2/R3**: unification is the P1 pre-step; the orchestrator loop is decommissioned in P7 (and only there, only after the P7 gate).
- No `Lagom` rename on this branch (R7 — named follow-up).
- No host-based project addressing / parent-domain cookie decision; no migration of rask's remote-function data layer to the BFF pattern (transitional coexistence).
- No `common`→`service-kit` convergence (deferred follow-up).
- No hybrid+rerank search build-out (still the acknowledged lancedb-SDK gap on both sides); no `/search` Tier-2 work beyond the pin-verification test.
- No license relabeling beyond restoring rask's original labels; the AGPL/Apache question is surfaced to the user, not decided.
- No git-history grafting; no pre-emptive cleanup of rask strays outside the P7 sweep commit.
