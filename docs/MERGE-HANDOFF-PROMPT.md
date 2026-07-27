# Handoff prompt v4 — the copy, in a fresh session

Part A (the restructure mend) is complete and externally verified from the tree (2026-07-27). This prompt
is written to be pasted into a FRESH rask session so the largest phase gets a clean context budget. Paste
everything below the line.

---

You are executing the copy phase of the lance-ns → rask merge, in `/home/blackwell/Desktop/rask`, branch
`feat/lance-ns-merge`. The authority is `docs/architecture/lance-ns-merge.md` (rulings R1–R11, decisions
D1–D7) — read it before touching anything. A prior session did the restructure; you inherit its tree, not
its memory.

## Standing constraints — non-negotiable

- **Never push rask to any remote. Never commit to or merge with rask `main`.** All work stays on
  `feat/lance-ns-merge`.
- **Never edit `/home/blackwell/Desktop/lance-ns`** — copy out only, taken fresh at copy time.
- Commit messages: plain conventional, **no trailers** (no Co-Authored-By).
- Secrets: app services consume from OpenBao via the Dapr secret store as strict sole source, fail-closed
  — never a k8s Secret, never plaintext env. Never delete the OpenBao pod (dev-mode, in-memory).
- Every claim cites command output or a screenshot; "looks right" is not evidence. Report an honest
  fraction at each gate; never mark a gate met on a check you did not run.
- A gate that can only observe the build step cannot see a failure in a step the build doesn't run. Treat
  "the command exited 0" and "the artifact is correct" as separate claims, and find a cheap observation
  for the second (the built-CSS byte-count A/B below is the reusable pattern).
- Backward compatibility does not matter — change to the right thing and update all callers.

## The state you inherit — verify from the tree (~60 seconds), then go

The prior session: dissolved `components/`, put deployables at `services/`, made root `packages/`
Python-only with glob members, sealed `runners/htr` OUT of the workspace with its own pyproject + lock,
deleted eslint+prettier (R10: oxlint + oxfmt + rsvelte-fmt won; bun-first), renamed the zone directory to
`frontend/microfrontends/` (R11), fixed the Tailwind `@source` climb in all 7 zones (41 KB of styling was
silently missing), recorded R11 + the lance-ns pin in the plan, rebased onto `origin/main`, and left ruff
+ ty green on a clean tree.

Verify each — from the tree, not from this prompt:

```bash
git status --short                                     # empty
git rev-list --left-right --count origin/main...HEAD   # 0 <tab> N — zero behind
ls frontend/microfrontends/                            # the rask zones; frontend/components/ gone
grep -rn "@source" frontend/microfrontends/*/src/app.css  # every climb '../../../packages/ui/dist'
grep -n "R11" docs/architecture/lance-ns-merge.md      # the ruling is recorded
test -f runners/htr/pyproject.toml && test -f runners/htr/uv.lock && echo sealed
uvx ty check && uv run ruff check .                    # both clean
```

If any check fails, STOP and reconcile before copying — do not build on an unverified inheritance.

# PART B — the copy: lance-ns in, ALL of it (R1, total merge)

**Source pin: `/home/blackwell/Desktop/lance-ns` at current `main` — re-pin to
`git -C /home/blackwell/Desktop/lance-ns rev-parse main` at copy time, record the SHA in the plan, and
take every copy fresh from that pin, never stale. (`6fbaa0e` + docs-only commits at authoring.)** Never edit that repo — copy out only. Never push rask to any remote;
never commit to or merge with rask `main`. **Copy-completeness gate:** after copying, diff the top-level
inventory — every item in `git -C …/lance-ns ls-files | cut -d/ -f1 | sort -u` (28 today) must exist in
the rask tree or appear in your commit message with the manifest row that transformed it. Nothing from
lance-ns is dropped silently.

## The zone set — who survives, who is eaten, who owns the top navbar (R6 + R8 + R9)

Six zones, all six in the top navbar: **`home · lakehouse · media · annotator · compute · studio`**.

- **rask's viewing / browse / search estate is EATEN by the lance-ns media plane.** The `discover` zone
  (browse + search), the EAD `/api/v1/catalog` endpoints, `search_api`, and `volumes_api`'s page/ALTO
  viewing all retire; lance-ns `viewer`/`search`/`annotator` services and the `media` + `annotator` zones
  replace them. What survives of `volumes_api` is only the `/objects` S3 browser — re-landing as a view
  *inside* `lakehouse`, not a zone.
- **`compute` survives as its own top-navbar zone** — Ray dashboard, jobs, actors, cluster. The plane rask
  owns.
- **`studio` survives as its own top-navbar zone** (R9) — not folded into anything; it already is one
  (`nav-config.ts`, its own `/animation` route).
- `storage` folds INTO `lakehouse`; `train` folds in via `models` (a lakehouse route); `overview` folds
  into `home`. One home: rask's home content folds into the incoming `home` zone, which owns
  `/auth/{login,callback,logout}` and the catch-all.

## The copy manifest — R1 is TOTAL; every top-level item has a destination

| lance-ns | → rask | Note |
|---|---|---|
| `frontend/` | `frontend/` — zones land at **`frontend/microfrontends/{home,lakehouse,media,annotator}`** (R11); the 7 `@repo/*` packages at `frontend/packages/*`; `package.json`, `bun.lock`, `turbo.json`, `knip.json`, `microfrontends.json`, `.oxlintrc.json`, `.oxfmtrc.json` at the `frontend/` root — **no path translation**: both trees are `frontend/microfrontends/` (R11; lance-ns renamed to match, `6fbaa0e`) | The JS plane root is `frontend/`, not the repo root (owner-ruled). rask's `compute` + `studio` merge in as zones; rask's `packages/{api,ui}` fold INTO `@repo/api`/`@repo/ui` (keep rask's storybook + `navMain(project)`). The incoming `@repo/zone-contract` (10 test files, 591 tests) REPLACES rask's 2-file stub in the same commit — it is the falsifiability layer for every frontend claim. Dev ports: incoming zones take fresh slots — lance `lakehouse` 5174 collides with rask `storage` 5174, lance `annotator` 5177 with `studio` 5177, and R9 keeps studio, so that one is live |
| `services/` (catalog, lineage, medallion, compaction, viewer, search, annotator) | `services/*` | src-layout conversion at copy; entrypoints preserved (`catalog.main:app`) |
| `packages/` (`common`, `ratch` — **ratch already moved**, `45912c8`) | `packages/*` | Workspace members. `common` keeps import root `common` (zero rewrites); `ratch` gains its own pyproject with its ray/lance-ray/typer deps at P1, resolving its old root-tooling exclusion |
| `runners/` (asr, diarize, kg, topics, voiceprint, assist — **already sealed**, `a4cf8f6`) | `runners/` top-level | **ABSOLUTE rule: a runner is NEVER a workspace member.** Each keeps its own pyproject + deps (+ `assist`'s own `uv.lock` and image). rask's `components/cli/runner` (HTR) also lands here as `runners/htr`, sealed, OUT of the workspace — no "resolves today" exception |
| `pyproject.toml` + `uv.lock` + `.python-version` | the workspace conversion — **all three stay at ROOT** | Root `[tool.uv.workspace] members = ["packages/*", "services/*"]` — GLOBS (runners matched by no glob). ONE lock at root, zero member-level locks (rask's existing shape — verified); members declare deps in their own pyproject but never lock. Root `.python-version` (3.13, both repos agree) is the fleet interpreter; uv discovery walks up, so members inherit — **a runner adds its own `.python-version` only when it diverges from the fleet** (the Ray-image case; today none do). Regenerate `uv.lock`; append `tests/unit`, `tests/integration`, `tests/e2e-py` to the explicit `testpaths` with a **collection-count assertion** — silent testpaths loss is rask's own risk 5 |
| `chart/` — **ALL of it** | grafted into `rask/chart/` | Per P4: subchart dedupe (rask's deps, lance-ns's richer values), every template incl. `dapr-statestore.yaml` and `runners.yaml`, all hooks with explicit component labels, NetworkPolicies ported AND extended over rask's fleet, `values-prod.yaml`, `prod_render_check.sh` adapted to `rask-` names |
| `deploy/` | three destinations | `cnpg-age-cluster.yaml` → `chart/templates/age-cluster.yaml` (AGE as a CNPG ImageVolume extension — decided and proven; needs K8s 1.33+); `kind/kind-config.yaml` → keep at `deploy/kind/` — `make kind-up` reads it and kind is this branch's proof vehicle; `ray-lance-demo.yaml` → copy transitional, retired at P5 by the unified RayCluster |
| `.docker/` — all dockerfiles | `.docker/` | P3: `rest-catalog` rewritten for src-layout (`uv sync --package`) and it must cover or split the media trio; `frontend.dockerfile` builds the six-zone set; `assist-runner` builds from `runners/assist`'s own lock; `cnpg-age-ext` as-is |
| `.dagger/` + `dagger.json` | merged into rask's module | One `dagger.json`; `TestPg`/`MigrateUp` untouched; source paths rewritten to the D7 tree |
| `.github/workflows/` | copied, paths adapted | Inert until a push exists; the merged Dagger module is the CI vehicle — local `make ci` / `make e2e-ci` / `make e2e-ray-ci` are the branch's enforcement, logged in the merge doc |
| `tests/` | `tests/unit`, `tests/integration`; Python live suites → `tests/e2e-py` | `tests/e2e` stays the `@rask/e2e` bun package |
| `scripts/` — all | `scripts/` | No name collisions (verified). Object names and ports adapted at P6: `rask-`/`rask-web-` prefixes, gateway `:8888` |
| `docs/` — **copy `OPEN-WORK.md` + `GOAL-UX-REACTIVE-EVIDENCE.md` FIRST, before any code** | `rask/docs/` + zensical nav | `OPEN-WORK.md` is the durable backlog; P8 reconciles it (closed items struck WITH evidence), never drops it. The evidence file is what merged-tree regressions get compared against |
| `lance_docs/` (vendored upstream Lance format docs, 171 files) | `rask/lance_docs/` | As-is |
| `.claude/` + `skills-lock.json` | merged into rask's `.claude` | Combine the skill sets; the `rask-*` skills are redrawn at P8 for the merged fleet |
| `Makefile` | merged | lance-ns's kind lifecycle (`bootstrap/kind-up/deps/images/load/deploy/up`, `e2e-ci`, `e2e-ray-ci`) lands ALONGSIDE rask's k3s targets |
| `Tiltfile`, `.devcontainer/`, `.hadolint.yaml`, `.dockerignore`, `.gitignore`, `.vulture-whitelist.py`, `.python-version`, `README.md` | carried / merged | Reconcile the dev loops (Tilt vs `dev-micro.sh`) at P6 — don't silently drop either |
| `LICENSE` | **surface, don't decide** | Risk 8: lance-ns carries an Apache relabel; rask's identity is AGPL-3.0-only. Restore rask's original labels and flag the question to the owner |

## The traps, in copy order

- **Tailwind `@source` climbs break SILENTLY when a directory moves — both trees are fixed today; the
  CLASS is not.** Each zone's `src/app.css` reaches `@repo/ui` by a relative climb
  (`@source '../../../packages/ui/dist'`). One `../` too many and Tailwind v4 simply stops scanning the
  package: no error, markup present in SSR, every `lg:*` utility silently unemitted. In lance-ns the
  estate sidebar collapsed to `display:none` and only an element-visibility assertion caught it, after a
  four-experiment bisect; in rask the same defect cost 41 KB (~48%) of the built stylesheet (fixed —
  A/B 44,796 → 86,011 bytes). After ANY directory-depth change:
  `grep -rn "@source" frontend/microfrontends/*/src/`, verify every relative target EXISTS from the
  file's own directory, then A/B the built CSS byte count — observe the artifact, not the exit code.

- **`rask-web-<zone>`** for every frontend k8s object. The live instance of the collision class: the
  `annotator` ZONE vs the `services/annotator` backend Service under one release.
- **The state store** (`dapr-statestore.yaml`): DSN from OpenBao through `lance-secrets`, never a k8s
  Secret; `scopes` must list every app owning operational state or saved work 503s with only a sidecar
  log to show for it — `tests/unit/test_invariants.py` pins the agreement; keep it collected.
- **Live SSE streams**: every zone shell holds one open. Traefik needs the equivalent of nginx's
  `proxy-read-timeout: 3600` or every zone reconnects on a timer, re-priming the event window and writing
  an audit record each time. Verify with `scripts/verify_live_stream_timeout.mjs`, `HOLD_S` past 255.
  Corollary: **`networkidle` can never fire in any zone** — `no-networkidle.test.ts` enforces it; wait on
  the element you act on, or assert the effect with `.toPass()`.
- **Helm**: numeric values use `hasKey` + `ternary`, never `| default` (an explicit `0` is swallowed);
  Job/CronJob pod templates need explicit component labels (prod default-deny blocks them; kindnet hides
  it); never `helm upgrade -f chart/values.yaml` against a live release — `helm get values` first.
- **kind**: after `kind load`, DELETE pods (rollout restart keeps the old digest) and verify the app
  container's `imageID` **by name** — `containerStatuses[0]` is the daprd sidecar on a 2/2 pod. Never
  delete the OpenBao pod: dev-mode, in-memory, every secret dies with it.
- **Gateway rows (P1)**: `/api/catalog→catalog`, `/api/lineage→lineage`, `/api/produce`+`/api/train→
  medallion`, the whole-plane media namespace `/api/media→viewer`, `/api/media/search→search`,
  `/api/media/annotations→annotator` (R5), plus catalog `user_state`/`me`/`access_admin`. lance services
  serve `/v1/...` internally — a wrong strip prefix 404s silently.
- **ratch**: its `cli/` still holds unwired repo-relative `from runners.…` imports (lance-audio
  heritage). They are replaced by the Ray-native name seam (`Stage.runner=` + worker `runtime_env` from
  the runner's own pyproject) — never resolved by making `runners/` importable again.

## Gates for this step

`uv sync` clean · `make check` (ruff format --check + ty, whole repo) · `make test` with the
collection-count assertion (≥67 unit + 13 integration files at the pin; re-derive at copy) ·
`turbo run check test lint fmt:check build` green from `frontend/` across the six-zone set · **the full
`@repo/zone-contract` suite collected and green** — it is the falsifiability layer; if it silently never
runs, every frontend claim is unguarded · helm lint + render invariants (incl. the rendered-name
uniqueness assertion) + `prod_render_check` · all images build and `kind load`.

Then the P6 global live drive on kind: `seed_medallion_fga.sh` + restart lance-ray (green e2e ≠
drive-ready), alice `/produce` 202 / bob 403 / anon 403 → cascade rows per stage → lineage populated →
cross-zone OIDC (sign in on `/lakehouse`, still signed in on `/media` and `/annotator`; alice 2xx / bob
403) → rask's `mfe.spec` green against the SAME deploy → DLQ view + replay → `prod_render_check` green.

Report an honest fraction at each gate. Never mark a gate met on a check you did not run. When a claim
can be settled by reading the tree or running one command, do that instead of remembering.
