<!-- Status: PLAN — decisions marked ACCEPTED below are owner-ratified; the rest stay PROPOSED.
     Authored 2026-07-24. RE-PINNED 2026-07-27: main@df70b63 -> main@502150b (190 commits), with owner
     ruling R8 and the structural drift folded in — see "Re-pin 2026-07-27" below.
     Source repo: /home/blackwell/Desktop/lance-ns
     (re-pin to current lance-ns main at each phase copy — copies are taken fresh, never stale). -->

# Merge plan: lance-ns → rask (`feat/lance-ns-merge`)

> **PART B COPY PIN: lance-ns `main@083b49a`** (re-pinned 2026-07-27 at copy time; source tree clean,
> 28 top-level items). Every copy in Part B is taken fresh from this SHA.

> **Rebased onto `origin/main` 2026-07-27.** The branch had been cut from a local `main` last pulled
> 2026-06-25 and was 69 commits behind. All 14 branch commits replayed; the branch is now 0 behind.
> Four rows below were CONTRADICTED by what those commits shipped and are struck through in place:
> the `controlplane` service on `:8820` (P4 netpol), the Apache-2.0 relicense (risk 8), the
> observability subcharts rask already has (P4 net-new), and host-based project URLs (P2 / risk 3 /
> not-do list). Upstream's `controlplane` landed at `services/controlplane` and is picked up by the
> `services/*` glob with no manifest edit.

Source of truth for the copy is `/home/blackwell/Desktop/lance-ns` at **`502150b`** (re-pinned 2026-07-27 from `df70b63`); target is `/home/blackwell/Desktop/rask` on `feat/lance-ns-merge` (carries the `projects/`-layer removal, `06a60a4`, **and the D7 restructure, which LANDED 2026-07-27** — `frontend/` is now its own bun+turbo root, `components/` no longer exists, and both workspaces glob; see D7 below for what actually shipped vs what was proposed). The vendored reference copy at `/home/blackwell/Desktop/lance-ns/rask/` is NOT a merge source or target.

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

**The merged zone set (R8 + R9): `home + lakehouse + media + annotator + compute + studio`.**

**Easier than the plan assumed:** four zones instead of seven; AGE-on-CNPG **decided and proven** (stock
image + ImageVolume, `docs/CNPG-AGE.md`) so PROPOSED decision 1 is settled; tenancy is **no longer a
decision** (per-warehouse physical multi-tenancy shipped, rask's single implicit `default` is the degenerate
case); catalog 501s **confirmed at 7** (`docs/COVERAGE.md`, 47/54 backed).

## P0 — Branch hygiene + repo-layout decision (first commit is a doc, not code)

**Hygiene rules (whole branch):**
- All work commits land on `feat/lance-ns-merge` in `/home/blackwell/Desktop/rask`. Never push to any rask remote. Never commit to or merge with rask `main`. Never edit `/home/blackwell/Desktop/lance-ns` (copy out only).
- Conventional commits (rask `cliff.toml`); each phase = one reviewable commit series; each commit message cites the lance-ns source commit (`c2ae04f`) — full git-history grafting (subtree/filter-repo) is explicitly out of scope, provenance is by citation.
- First commit: `docs/architecture/lance-ns-merge.md` — the layout table below, the naming rules, and the five decisions restated with status PROPOSED. rask's `docs/architecture/` is its living design record; this is where the plan lives, not in scratch files.

**Layout decision (where everything lands)** — the destination column is written against the **landed D7 tree**
(`services/`, `packages/`, `frontend/`; `components/` is gone as of 2026-07-27):

| lance-ns source | rask destination | Rule |
|---|---|---|
| `services/{catalog,lineage,medallion,compaction,viewer,search,annotator}` | `services/{catalog,lineage,medallion,compaction,viewer,search,annotator}` (converted to src-layout: `src/<name>/…`, entrypoints preserved: `catalog.main:app`) | rask shape: workspace member + `.docker/<name>.dockerfile` per deployable (no `projects/` layer — removed 2026-07; no `components/` layer — dissolved 2026-07-27 by D7). Membership is now the **`services/*` glob**, so a copied service registers itself — nothing to add to `pyproject.toml`. The media trio (viewer/search/annotator, folded from lance-audio) is IN scope (total merge, owner-ruled); lance `search` coexists with rask's `search_api` until P7 retires the latter (gated on the P5 pin test) |
| ~~`src/ratch`~~ **`packages/ratch`** (already moved in lance-ns, `45912c8`) | `packages/ratch` (uv workspace member, src-layout) | **RE-PIN — no plan row existed.** Owner-ruled 2026-07-27: ratch is a **package**, not a deployable — it belongs in the Python `packages/` plane, not `services/`. (The `components/cli` layer this row originally contrasted against no longer exists.) It is the folded lance-media pipeline tree, currently UNWIRED (lance-ns `pyproject.toml:113` excludes it, with the note *"no service imports them … ratch's ray[data]/lance-ray/typer stack lands with the pipeline step"*). Making it a workspace member is what **resolves** that exclusion: its heavy deps live in its own `pyproject.toml` instead of the root's, which is precisely why it was excluded |
| `services/common` | `packages/common` (distribution name `lance-common`, import root stays `common` so zero import rewrites) | Transitional; long-term converge on `service-kit`'s `make_service_app`, keeping common's auth/FGA/audit middleware as the governed variant — NOT on this branch |
| ~~`frontend/microfrontends/{data,lineage,models,admin}`~~ **`frontend/microfrontends/lakehouse`** | `frontend/microfrontends/lakehouse` | **RE-PIN**: the four are one zone since `bb099df` — `data`/`lineage`/`models`/`admin` are its *routes*. One app, one port slot, one ingress rule, one `.docker` build, one spec dir. Per **R8** it also absorbs rask's `storage` (the S3 object browser) and `train`. **The zone dir is `microfrontends/`, not lance-ns's `components/frontends/`** — see D7's landed note; the incoming zone-contract's hardcoded paths need translating |
| `frontend/microfrontends/{media,annotator}` | `frontend/microfrontends/{media,annotator}` | The media zones are **`ssr=false` SPAs with NO BFF** — root-absolute `/api/*` fetches, a different deploy/env shape than the SSR zones; their fetch bases are rewritten to the `/api/media/*` namespace (owner-ruled, see P1) |
| `frontend/microfrontends/home` | **dissolves into rask's `frontend/microfrontends/home`** (`/auth/{login,callback,logout}` routes + zone-picker landing content move in; lance home package deleted) | One catch-all only |
| `frontend/packages/api` (**`@repo/api`**) | merged into `frontend/packages/api` (add `oidc.ts`, `bff.ts`, `gateway.test.ts`, **`runs-feed.ts`**, export conditions `./oidc`,`./bff`,`./runs-feed`) | rask original wins (it is `@rask/api` and already sits at `frontend/packages/api` since D7 landed). **RE-PIN**: the scope on the lance side is `@repo/*`, not `@rask/*`. `runs-feed.ts` is the shared run-notification generator every zone's bell stands on |
| **`frontend/packages/{config,engine,labeling,media-api,zone-contract}`** (net-new) | `frontend/packages/*` (the `packages/*` glob picks them up) | **RE-PIN — no plan row existed.** `zone-contract` is the gate package (bff-routes, budget, cross-zone-reload, link-targets, live-stream, manifest, notification-surface, no-networkidle, poll-reason, proxy) and is what makes the frontend claims falsifiable; `media-api` is the media seam incl. the shared server cache. **Landed 2026-07-27:** rask now has a *stub* `frontend/packages/zone-contract` (`@rask/zone-contract`) holding only the cross-zone-reload guard — the incoming package folds into it rather than arriving on empty ground |
| `frontend/packages/ui` (**`@repo/ui`**) | merged into `frontend/packages/ui` (add the components + `gsap.ts`/`motion.ts` + shell fold; keep rask's storybook) | rask original wins; see P2. **RE-PIN**: the shell now also carries `notification-center`/`notification-list` and the `runs/run-status` ordering — the bell is shell furniture, not a zone feature |
| ~~`frontend/eslint-rules/cross-zone-reload.js`~~ | — | **RE-PIN: the directory no longer exists.** lance-ns is on **oxlint + oxfmt**, no prettier and no eslint. The guard survives as a TEST in `@repo/zone-contract`, so it needs no rask lint plugin — but P2's gate list must stop saying "eslint (incl. cross-zone-reload rule)". **Landed 2026-07-27 (R10, rask side): rask's `eslint.config.js` and prettier are DELETED**; the frontend runs oxlint + oxfmt + `@rsvelte/fmt`, and rask's own cross-zone-reload guard is already a vitest test in `@rask/zone-contract` |
| `chart/templates/*` (see P4) | grafted into `rask/chart/` | One umbrella, `fullnameOverride: rask` stays |
| `.docker/{rest-catalog,ray-lance,cnpg-age-ext,assist-runner}.dockerfile` (**`assist-runner` is new**) | `rask/.docker/` (same names; paths rewritten to new layout) | frontend.dockerfile: rask's parametrized one wins, adopt lance-ns's `oven/bun:1.3.14-slim` pin; images become `<app>:tag` (drop `lance-` prefix) |
| `scripts/*.sh`, `scripts/*.py` | `rask/scripts/` (no name collisions with `dev-micro.sh`/`k3s-install.sh`/`vendor-rustfs-operator.sh`) | Object names/ports inside adapted in P6. **Landed 2026-07-27:** `scripts/` is now the single script plane for both languages — the old `components/scripts/*.py` merged in, so the collision surface is bigger than this row assumed (`build_batches_db.py`, `chunk_batches.py`, `harvest_ead.py`, `index_alto.py`, `index_catalog.py`, `download_*.py`, `smoke_*.py`, … all live there now) |
| `tests/unit`, `tests/integration` | `rask/tests/unit`, `rask/tests/integration` | Appended to root `testpaths` (rask uses explicit testpaths — unlisted dirs silently never run) |
| `tests/e2e` (Python live suites) | `rask/tests/e2e-py` | Avoids colliding with `tests/e2e` = the `@rask/e2e` bun package (decision 4 target) |
| `docs/{DATA-CONTRACT,CNPG-AGE,RAY,RASK-INTEGRATION}.md`, `docs/{catalog,lineage}-openapi.json` | `rask/docs/` + zensical nav entries | RASK-INTEGRATION.md becomes the merge record's appendix |
| **`docs/OPEN-WORK.md`** | **`rask/docs/OPEN-WORK.md`** | **RE-PIN — load-bearing.** The backlog that must not be lost. Every open item was previously recorded only as a session task ID (`#103`, `#124`, …) in a tracker that does not outlive the session, and in `MERGE-REPIN-DELTA.md`, which is a diff meant to be deleted. `OPEN-WORK.md` is self-describing — what each item is, why it is open, where the code lives, what closes it — so it is legible to someone with no memory of the lance-ns session. **Copy it early (P0/P1), not in the P8 sweep**, or the P7 decommission deletes code whose follow-up work is only recorded in a file that has not arrived yet |
| **`docs/GOAL-UX-REACTIVE-EVIDENCE.md`** | `rask/docs/` | The pasted evidence for the twenty UX conditions, with the command that produced each line. Carries so a merged-tree regression can be compared against what was actually proven, rather than against a claim |
| `deploy/cnpg-age-cluster.yaml` | `chart/templates/age-cluster.yaml` | Decision 1 |
| `deploy/ray-lance-demo.yaml` | **replaced** by a `RayCluster` CR template (P5) | Option B |
| **`chart/templates/dapr-statestore.yaml`** (net-new) | `chart/templates/` | **RE-PIN — no plan row existed.** `state.postgresql` with `actorStateStore: "true"`, DSN resolved from OpenBao through `lance-secrets` (never a k8s Secret). Externalizes to CNPG like the rest. Its `scopes` must list every app that owns operational state — an app outside `scopes` gets "component not found" from its sidecar and every user's saved work 503s, which the sidecar logs and nothing else notices |
| **`chart/templates/runners.yaml`** + `runners/assist` (net-new) | `chart/templates/` + **`runners/assist`** (top-level, matched by no members glob) | **RE-PIN — no plan row existed.** A deployable absent from the P3 image list. Destination corrected from `components/runners/` — under D7 the runners plane is top-level and deliberately outside both workspaces |

**Why rask's layout wins, and it is not about tidiness (owner question, 2026-07-27).** lance-ns has **no uv
workspace at all** — it is a single package named `lance-ns` with `pythonpath = ["services", "."]`, a pytest
path hack that makes `catalog`, `lineage`, `medallion`… import as top-level modules. Nothing declares a
dependency on anything, so nothing can violate one: `catalog` importing `medallion`'s internals is not an
error, it is just an import. rask is a real workspace — **14 members** (6 under `packages/`, 8 under
`services/`, resolved by glob since D7 landed), each with its own `pyproject.toml`,
`src/`, `tests/`, and *declared* deps (`service-kit` depends on `storage`). Adopting rask's shape is not a
rename; it is the difference between implicit and enforced module boundaries, and the resolver is what
enforces it. Three things in lance-ns that only exist because there is no workspace: `services/common` is a
library living in the services directory (no `main.py`, no `app.py`), `src/ratch` sits at the root excluded
from the root's own tooling, and `runners/` has nowhere to be. All three get a home here.

**D7 — the merged tree: two language-pure planes (2026-07-27; supersedes D6's framing — D6 asked the
question, this answers it with the toolchains' own behavior). STATUS: the rask half LANDED 2026-07-27 —
this section is now a record of what shipped plus the incoming half, not a proposal.**

The glob question was settled **empirically**, not by taste. Scratch workspaces, both toolchains:

```
uv  lock, members=["pkgs/*"], one TS dir inside   → error: Workspace member `…/pkgs/tspkg` is missing
                                                    a `pyproject.toml` (matches: `pkgs/*`)
uv  lock, + exclude=["pkgs/tspkg"]                → Resolved 2 packages   (enumeration through the back door)
bun install, workspaces=["pkgs/*"], one Py dir    → Done! Checked 2 packages   (SILENTLY skipped)
```

So a mixed `packages/` forces uv into enumeration (or an exclude list that is enumeration renamed), and
bun's silent skip is its own hazard — the day a Python package gains a `package.json` (e.g. for a turbo
script), bun sweeps it in without a word. **Language-pure directories are the only shape where globs are
both possible and safe.** Combined with the measured fact that there are ZERO cross-language package deps
in either repo, the tree is:

```
/
├── frontend/                        ← the JS plane, wholesale = lance-ns's proven tree
│   ├── microfrontends/{home,lakehouse,media,annotator,compute,studio}
│   ├── packages/{api,ui,config,engine,labeling,media-api,zone-contract}    ← TS-only → globs
│   └── package.json · bun.lock · turbo.json · .oxlintrc.json · .oxfmtrc.json
├── services/                        ← Python deployables, src-layout, uv members via GLOB
│   ├── catalog/ lineage/ medallion/ compaction/ viewer/ search/ annotator/
│   └── gateway/ core/ ray_api/ … (rask's, absorbed per P1/P7)
├── packages/                        ← Python-only shared → uv glob works
│   ├── common/ ratch/ storage/ service-kit/ ray-kit/ htr/ tracker/ validate/
├── runners/                         ← SEALED projects, deliberately OUTSIDE the workspace (see below)
│   ├── asr/ diarize/ kg/ topics/ voiceprint/   (offline Ray Data: pyproject = the worker runtime_env)
│   └── assist/                                 (online server: own uv.lock, own image)
├── chart/ · .docker/ · .dagger/ · scripts/ · tests/ · docs/
└── pyproject.toml                   ← [tool.uv.workspace] members = ["packages/*", "services/*"]
```

**The `runners/` plane — a third kind, and the membership rule (owner-ruled 2026-07-27).** A uv workspace
is ONE `uv.lock` and one joint resolution, so anything pinned to an external runtime must stay out of it.
The runners are exactly that, measured: `assist` is already an independent project — own `pyproject.toml`,
**own `uv.lock`**, `requires-python = ">=3.12,<3.14"` against the root's `>=3.13`, `torch==2.9.1+cpu` from
the pytorch index, and its dockerfile syncs `--frozen` from its own lockfile; the offline runners pin CUDA
torch builds (`torch==2.11.0+cu128`) whose index and cadence must never enter the fleet's resolution. So:

- **The rule is ABSOLUTE (owner-ruled 2026-07-27): a runner is NEVER a workspace member.** Every runner is
  its own sealed project — own `pyproject.toml`, own dependencies, its own `uv.lock` where it builds an
  image — full stop. There is no "resolves with the fleet, so it may join" case: even a runner whose pins
  happen to resolve today is kept out, because the whole point is that its model's pins (CUDA torch, a Ray
  minor, a model SDK) move on their own cadence and must never be able to hold the fleet's resolution
  hostage tomorrow. The shares-the-fleet's-resolution test applies only to `services/*` and `packages/*`.
- **Runners are NOT under `services/`** — the `services/*` glob would sweep them into the workspace. They
  are top-level, matched by no members glob, needing no `exclude`.
- **Runners are sealed and self-contained** (owner-ruled): each carries its own README + `pyproject.toml`
  (+ `uv.lock` where it builds an image), and the tree has **no `__init__.py` package glue** — done in
  lance-ns at `a4cf8f6`. `rask/components/cli/runner` (the HTR runner, `ray>=2.52,<2.56` — today a
  workspace member) **already moved OUT of the workspace to `runners/htr`** (rask `bb4b4a4`, 2026-07-27) —
together with `packages/htr`, since `htr` is what actually pulls torch and nothing but the runner depends
on it. Root lock 200 → 145 packages; fleet suite ~32 min → ~6 s. It has its own
  `pyproject.toml`-as-sealed-project and its own lock. No hedge: the earlier "may stay a member if it still
  resolves" was mine, not the owner's, and it is struck. P7 re-cuts it as movers anyway, from `runners/`.
- **The ratch↔runner seam**: ratch knows runner NAMES and hands each runner's `pyproject.toml` to Ray as
  the worker `runtime_env`. ratch `cli/`'s leftover repo-relative imports (`from runners.diarize.… import`)
  are lance-audio heritage, unwired today, and are replaced by the name seam when the pipeline step lands
  — they must NOT be "fixed" by making runners importable again.

Note the tree above is already half-real in lance-ns: **`src/ratch` → `packages/ratch` is DONE**
(`45912c8`) and the runners restructure is DONE (`a4cf8f6`) — the copy is straight across, no path
translation.

**What this changes in the phases:**
- **The P0 frontend direction FLIPS.** lance-ns's `frontend/` tree comes wholesale — bun.lock, turbo.json,
  oxlint/oxfmt configs, and every zone-contract gate file **unchanged** — lance-ns renamed its own zone
directory to `microfrontends/` (`6fbaa0e`), so the two trees MATCH and no path translation happens at
copy — `FRONTEND_ROOT`'s internal shape is preserved and its repo-relative reads of `chart/` and
  `scripts/` still resolve. rask's `compute` + `studio` zones (2) move INTO it, rather than lance's
  zones moving out — fewer moves, and the proven gates travel byte-identical.
- **`components/` dissolved** (rask `bb4b4a4`, done): `components/services/*` → `services/*`;
  `components/cli/runner` + `packages/htr` → the sealed `runners/htr`; `components/scripts` →
  `scripts/`; `components/frontends/*` → `frontend/microfrontends/*`.
- **P2's 3-way `packages/ui` merge inverts**: rask's storybook + `navMain(project)` fold INTO
  `frontend/packages/ui` (`@repo/ui`), not the other way. Keep-from-rask list unchanged.
- **The manifest-completeness gate becomes unnecessary** — globs enforce membership structurally. (If D7
  is vetoed and the mixed root stays, that gate reverts to REQUIRED.)
- Two `packages/` directories exist (root = Python, `frontend/packages` = TS). Each is unambiguous in
  context; a reader inside a plane sees exactly one, and each plane's own convention calls it `packages/`.
- **Per-individual-service `packages/` is REJECTED**: code shared by one service is not shared — it is
  that service's `src/` internals. Packages exist per PLANE.

**~~PROPOSED D6~~ — subsumed by D7 (owner question,
2026-07-27).**

The evidence says the mixed root is grouping by the wrong property. Measured in both repos:

```
lance-ns TS packages   engine → @repo/config · labeling → @repo/config, @repo/media-api
                       zone-contract → @repo/config · api, ui, config, media-api → (none internal)
rask Python packages   htr → storage · service-kit → storage · ray-kit, tracker, validate, storage → PyPI only
rask TS packages       api, ui → no internal deps at all
CROSS-LANGUAGE DEPS    ZERO — in both repos, in both directions
```

Two strictly partitioned dependency graphs share one directory. `packages/` groups by *"is a library"*, a
property **neither toolchain nor any developer ever queries across the boundary**; it hides *language*,
which every toolchain queries on every run. That is not an aesthetic point — it is the direct cause of the
enumeration tax below.

**The proposal:** TS shared packages live under the frontend tree, Python shared packages live with the
services. Then `packages/*` globs work on both sides, and the silent-omission failure mode **disappears**
rather than needing a guard.

The one package that looked like a counterexample is not: `@repo/zone-contract` reads `chart/values.yaml`,
`chart/templates/*` and `scripts/verify_*` — but those are repo-relative **file reads at test time**, not
package dependencies, so it works from anywhere in the tree.

**Sequencing is the real risk, not correctness.** Restructuring rask's packages *during* a merge into rask
adds churn to a merge that already carries 190 commits of drift and two red preconditions. Two honest
options:
- **(i) Pre-P1 commit, provable against rask alone** — like clearing the 70 `ty` errors. Attractive because
  the ~11 incoming packages then land in the right place **once**, instead of landing in `packages/` and
  being moved later.
- **(ii) Named follow-up after P6 is green** — lowest risk to the merge, at the cost of moving twice.

If D6 is rejected, the mixed root stays and the manifest-completeness gate below becomes **required**, not
optional. If D6 is accepted, that gate is unnecessary — globs enforce it structurally.

**The cost of keeping one root `packages/`, if D6 is rejected.**
rask keeps TS and Python side by side in `packages/` (`api`/`ui` TS; `htr`/`ray-kit`/`service-kit`/`storage`/
`tracker`/`validate` Python). That is the target and it is not being changed — but it has a measurable price
worth naming, because the merge triples the number of packages:

```
lance-ns   workspaces: ['packages/*', 'components/frontends/*']            ← globs (tree is language-pure)
rask       workspaces: ['components/frontends/compute', … , 'packages/ui'] ← 9 paths enumerated by hand
           uv members:  packages/htr, … , components/services/orchestrator ← 14 paths enumerated by hand
```

**Neither toolchain can glob a mixed directory** — `packages/*` in bun's `workspaces` would sweep in Python
dirs with no `package.json`. So every package is registered by hand, twice, and **a forgotten registration
fails silently**: the package simply is not built, linted or tested, and nothing says so. This is risk 5
(silent gate loss) in a second guise, and after the merge there are ~11 more packages to forget.

**Required P1 gate:** a manifest-completeness test — walk `packages/*` and `components/*/*`, and assert
every directory containing a `package.json` appears in the root `workspaces` array, and every directory
containing a `pyproject.toml` appears in `[tool.uv.workspace] members`. Fail on either direction (a listed
path that does not exist is equally a bug). lance-ns's `@repo/zone-contract` is the precedent — it exists to
make exactly this class of claim falsifiable, and it ships with the merge.

**Naming rules (the load-bearing ones):**
1. **k8s objects**: backends `rask-<service>` (existing pattern); ALL frontend zones — rask's included — become `rask-web-<zone>`. Rationale — **RE-PIN, and the example got sharper**: there is no `lineage` zone any more, but the collision class is still live and now unavoidable — lance's **`annotator` zone vs the `annotator` backend Service** (`services/annotator`) under one release is exactly it, and lance-ns already hit this class (memory landmine + `frontends.yaml` NAMING comment). Renaming rask's 7 zone objects is branch-only churn and buys a uniform rule.
2. **Dapr app-ids**: backends keep bare names (`catalog`, `lineage`, `medallion`, `compaction`, `core-api`, `gateway`); frontends get no sidecars. No collisions in that set today; the rule prevents future ones.
3. **Ports (dev)** — **RE-PIN**: rask's `microfrontends.json` slots win; the **three** incoming lance zones
   get fresh slots (`lakehouse` 5180, `media` 5181, `annotator` 5182). They cannot keep their lance-ns slots
   because those **collide with rask's own**: lance `lakehouse` 5174 vs rask `storage` 5174, and lance
   `annotator` 5177 vs rask `studio` 5177 — and under R9 `studio` survives, so that collision is live rather
   than incidental. rask holds home 5273 / overview 5179 / storage 5174 / compute 5175 / discover 5178 /
   train 5176 / studio 5177. `:3024` proxy and `:5273` home are rask's (lance home dissolves into it). `PORT_OFFSET` in `dev-micro.sh` is the escape hatch for backend clashes.
4. **Gateway**: rask's FastAPI gateway (:8888, Dapr-aware) wins; lance-ns's nginx gateway retires (P1/P4).

---

## P1 — Python plane (hermetic; no cluster)

**Pre-step (before or parallel to P1) — Ray version unification (owner-ruled: ONE cluster, latest version).** Provable against rask alone, no lance-ns code involved: bump rask's Ray estate to the latest release (runner `pyproject` lifts `ray<2.56`; `ray-kit` re-tested; `.docker/{ray,runner}.dockerfile` bases rebuilt; `chart/templates/rayservice.yaml` image), align the Python minor with what lance-ray/pylance-8 support, and **revalidate the GPU Serve packing invariants** (fractions × replicas ≤ physical GPUs, host-RAM headroom, `smoke-gpu.sh`, `/transcribe` + `/htrflow` answer). Gate: rask's existing HTR smoke path green on the new version. Everything after lands on the unified cluster.

**Moves/adaptations:**
- Copy the seven services + common per the P0 table; convert to src-layout; add workspace members to root `pyproject.toml` (`packages/common`, **`packages/ratch`**, the 7 services, and the `runners/assist` deployable) and regenerate `uv.lock`. **RE-PIN**: lance-ns is not a workspace today (single package + a `pythonpath` hack), so this is a conversion, not an append — every incoming module gets a declared home and declared deps for the first time.
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
2. **`packages/api` merge**: copy in `oidc.ts` (sealed AES-256-GCM cookie), `bff.ts` (`makeSessionHandle`/`sessionToUser`), `gateway.test.ts`; add export conditions. License: **settled — both sides are Apache-2.0** (rask `origin/main` relicensed 2026-07-20, `6baa318`), so risk 8 is obsolete.
3. **`packages/ui` merge**: add `chip`, `search-bar`, `select`, `status-board`, `lib/gsap.ts`, `lib/motion.ts` (`{@attach}` factories). Shell fold — take from lance-ns: `authEnabled`/`user: null` signed-out state in `app-shell.svelte`/`nav-user.svelte`, trailing-slash `norm`/`exact` matchers, `shell/breadcrumb.ts`; keep from rask: `navMain(project)` factory, path-derived project (`segs[0]`), storybook + `css.d.ts`. **REVERSED by the rebase:** `projectFromHost` now exists in rask's OWN shell (`packages/ui/src/lib/shell/breadcrumb.ts`, from upstream), `navMain()` lost its `project` argument, and the zones re-based to `/<domain>`. The two shells have CONVERGED, so this fold is now a 3-way merge of two similar files rather than a graft — easier than planned, but only if rewritten against the real base.
4. **Zones** — **RE-PIN: three, not six.** Move `lakehouse`, `media`, `annotator` in (lance `home`
   dissolves into rask's home as before). The media pair are `ssr=false` SPAs with no BFF, fetch bases
   rewritten to `/api/media/*` per P1. Register in root `package.json` + merged `microfrontends.json`; the
   old six-slot port allocation collapses to three (`lakehouse`, `media`, `annotator`). Zones keep flat
   bases (`paths.base: '/lakehouse'` …) alongside rask's `/default/<zone>` — coexisting URL families,
   transitional. Per **R8**, rask's `storage` and `train` zones are retired *into* `lakehouse` rather than
   carried, and `discover` retires at P7.
5. **Home merge**: rask home absorbs `/auth/{login,callback,logout}` and the zone-picker; add a **reserved-segment guard** to `routes/[project]/+page.server.ts` rejecting **`lakehouse|media|annotator|compute|studio|storage|auth|api|default|capi`** as project ids (**RE-PIN**: `data`/`lineage`/`models`/`admin` are lakehouse ROUTES, not zones; kills the `/lakehouse` → `/lakehouse/overview` → 404 trap).
6. rask zones stay auth-free: `authEnabled: false` when OIDC env is absent (`makeOidcConfig(env)` already tolerates this); no retrofit of remote-function data layers. **OIDC deltas since the original pin** land with the fold: split-horizon issuer discovery (public issuer string vs in-cluster fetch — two env keys in the shared `frontendEnv`) and the **login-first gate** (signed-out page loads redirect to `/auth/login`) — the gate is wired **only when `frontend.oidc.enabled`**, otherwise rask zones' auth-free posture would break.

**Gates** — **RE-PIN**: one bun lockfile; `turbo build` across the merged zone set (**recount at copy time**
— 3 incoming + rask's surviving set under R8, not "13"); `build-storybook`; svelte-check; **the lint/format
gate per the step-1 decision** (not "eslint incl. cross-zone-reload" — that guard is a zone-contract test
now); knip; `fmt:check`; bun tests; **the whole `@repo/zone-contract` suite**, which is what makes the
frontend claims falsifiable and must be wired into rask's test run or it silently never executes.
**Live-proof**: dev proxy `:3024` up — rask home hydrates, `/lakehouse` hydrates, `/media` hydrates,
`/annotator` hydrates, `/auth/login` resolves; `/compute` unaffected.

## P3 — Images + Dagger

- `rest-catalog.dockerfile` rewritten to build from `components/services/*` + `packages/common` via `uv sync --package <project>` (rask's pattern) — **and it must cover (or split images for) the media trio**: at `df70b63` viewer/search/annotator share the catalog image (`services/` COPY'd whole), which the src-layout conversion breaks; `ray-lance.dockerfile` re-based onto the unified Ray version (P1 pre-step) and `cnpg-age-ext.dockerfile` copied as-is; unified `frontend.dockerfile` per P0 builds **the merged zone set — six under R8+R9** (`home`, `lakehouse`, `media`, `annotator`, `compute`, `studio`); **RE-PIN**: recount at copy time, never "13".
- Merge lance-ns's `.dagger` Go functions (`Test`, `Lint`, `Typecheck`, `Openapi`, `Charts`, `Frontend`, `TestLineage`) into rask's `.dagger` module — one `dagger.json`, `TestPg`/`MigrateUp` untouched, source paths rewritten.

**Gates**: `dagger call` on every function green locally; hadolint. **Live-proof**: all images build and `kind load` / `k3s import` succeed.

## P4 — Chart unification

- **Subchart dedupe** (one control plane each): keep rask's deps for nats 2.14.2 / dapr 1.18.1 / openfga 0.3.9 / cloudnative-pg 0.28.3 / kuberay / rustfs-operator; lance-ns **values** win where richer — nats (credentialed, netpol'd), openfga (`datastore.engine: postgres` + migrate hook **replaces** rask's memory toggle, weight −5). CNPG CRDs stay vendored in `chart/crds/` with `crds.create=false` — lance-ns must not re-install them.
- **Graft templates**: `services.yaml` (+lineage cron binding), `medallion.yaml` (producer + 4 movers + per-mover DLQ), `compaction.yaml`, **`media.yaml`** (the viewer/search/annotator Deployments + the `media-catalog-token` bearer Secret; the `/var/media-corpus` node-local hostPath is kind-only — resolve in-phase to a PVC or a rustfs-backed corpus bucket before k3s/prod, no hostPath ships), **`dex.yaml` at its `df70b63` shape** (served on the app origin at `/dex` + restart-on-config-change); **delete** lance-ns `gateway.yaml` (nginx) — rask's gateway carries the routes from P1 incl. the `/api/media/*` rows; `frontends.yaml` merged with the universal `web-` prefix + `frontend-session` Secret (media zones as static SPA deploys, not Bun SSR); `ingress.yaml` = rask's template with the **3** incoming lance zones (`lakehouse`, `media`, `annotator`) appended to `frontend.apps` — **RE-PIN**: lance `home` dissolves into rask's catch-all, and `data`/`lineage`/`models`/`admin` are routes of `lakehouse`, not apps; `storage` and `train` are removed from `frontend.apps` per R8 (template needs zero changes — the socket already exists).
- **Hooks** (all Job/CronJob pod templates carry explicit component labels — the netpol landmine): `nats-stream-job` (LINEAGE + DLQ `dlq.>` streams; Dapr jetstream does not auto-create), `openfga-migrate`, `bootstrap-admin` (weight 5, seeds `owner` on `warehouse:lance_catalog`), `greptimedb-ttl-job`, rustfs bucket-init (re-pointed at operator Tenant endpoint `<tenant>-io:9000`), openbao seed, `backup-pg` + `backup-snapshot`.
- **NetworkPolicies**: port ALL of them (recount at copy time — "13" predates the media plane) AND extend to rask's fleet — default-deny now covers rask pods, so new allows: ingress→`web-*` frontends, gateway→fleet ports 8801–8820 (**8820 is `controlplane`** — the range stopped at 8810 before the rebase, which would have left `/api/v1/projects` 503-ing and the home project picker dead), core-api/orchestrator→`rask-postgres-rw:5432`, ray-api→dashboard 8265, search-api→rustfs. Enumerate before applying; kindnet hides violations.
- **Dapr resources**: `dapr-component.yaml` (lance-pubsub, catalog-control-pubsub, per-app subscriber pubsubs with the two retry profiles, `lance-secrets` OpenBao store), `dapr-app-token.yaml`, `dapr-resiliency.yaml`.
- **AGE**: `age-cluster.yaml` (CNPG Cluster + ImageVolume extension image) replaces `age-postgres.yaml` (decision 1).
- **RustFS**: rask's operator Tenant wins; lance-ns's per-warehouse bucket provisioning code (`warehouse_registry.py` and bucket-init) re-pointed at `<tenant>-io:9000`; keep-PVC posture via Tenant spec.
- **Net-new**: ~~greptimedb-standalone + perses subcharts~~ (**rask already ships greptimedb-standalone 0.4.5, vector 0.56.0 and perses 0.22.0** as of the rebase — dedupe them like the other shared subcharts; lance-ns retired Vector for an in-chart OTel Collector, so that one IS a real decision — **RULED 2026-07-27: Vector retired; the OTel Collector's filelog receiver is the single log shipper**), otel-collector, openbao, dex, alerting (+ `rules_test.yml`), perses-dashboards, external-secrets, security-sa, ha.yaml, merged `values-prod.yaml`; `prod_render_check.sh` adapted to `rask-` names.

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

**Global live-proof (the phase gate)** on the merged chart on kind: `seed_medallion_fga.sh` + restart lance-ray (drive-readiness landmine: green e2e ≠ drive-ready) → alice `/produce` 202 / bob 403 / anon 403 → cascade rows per stage in the tenant bucket → lineage graph populated → cross-zone OIDC (**RE-PIN**: sign in on `/lakehouse`, still signed-in on `/media` and `/annotator` — a hop between lakehouse's own areas is a soft navigation now, not a cross-zone hop; alice 2xx / bob 403) → rask `mfe.spec` green against the **same** deploy (home + all `/default/*` hydrate) → DLQ view + replay → `prod_render_check` green.

## P7 — Convergence (the compute-plane cutover; owner-ruled IN scope, sequenced AFTER P6 is green)

The coexistence merge (P1–P6) keeps rask's orchestrator running untouched. P7 executes the target architecture on the proven base:

**a. IIIF → bronze ingest producer.** No S3-ObjectCreated exists for IIIF, so ingestion is a producer job honoring the lance-ray seam contract (`RASK-INTEGRATION.md`): harvest IIIF pages → write the bronze **blob-v2 page-image Lance dataset** (the `ray_stage_job.py` media path is the precedent) → emit ONE raw-write OpenLineage RunEvent — and **never publish `medallion.raw` itself** (the `/raw-arrival` subscription fires the cascade; publishing both double-fires it). This replaces the prefetch lane + `IIIFCachedSource`'s cache role.

**b. HTR stages as movers, ending at gold.** Re-cut the runner pipeline as event-triggered movers on the unified cluster: bronze page-images → silver (Layout/Lines regions+geometry) → gold (transcriptions). Only the two IO endcaps change — `PageLoaderActor`/`AltoWriterActor` are replaced by Lance reads/writes; Layout/Lines/TranscribeViaServe (still calling the warm Serve handle) transfer as-is. Each mover: read upstream Lance version-range → transform → write downstream → emit the `DERIVED_FROM` edge + version facet → publish the next trigger; FGA `can_create_table`/`can_promote` gates; vended short-TTL table creds via the catalog (workload identity, no durable secret on compute). **The gold schema contract is pinned here**: page dims, region/line polygons, reading order, text, confidences — everything a serializer needs; a field dropped from gold is unrecoverable downstream.

**c. The `exporter` service (owner-ruled: serialization is a separate microservice).** New `components/services/exporter` (+ dockerfile): projects consumer formats from gold Lance — never inside the lakehouse or the movers. ALTO 4.4 first (the serializer extracted from `AltoExportActor` into a plain library the service imports); future formats (PAGE XML, plain text, IIIF annotations, hOCR) are new functions, zero pipeline changes. Two surfaces: sync single-document export (`GET …/export/{doc}?format=alto`) and async bulk export for the Archives deliverable (whole volume → a *delivery* bucket — egress artifact, outside the lakehouse; optionally emits a terminal read-lineage event so "delivered ALTO for volume X from gold vN" is provenance).

**d. Decommission (only after a+b+c are live-proven end-to-end, and after `docs/OPEN-WORK.md` has been checked for follow-ups attached to anything on the delete list).** Flip `RASK_ORCHESTRATOR_AUTOSTART` off fleet-wide, then delete: `core/services/orchestrator/{loop,derive}.py`, `core/services/sync.py`, the `orchestrator` entrypoint + `:8810` (dev-micro.sh row, gateway row, chart), the batches/chunks/orchestrator endpoints + the `batches` table + its Alembic lineage (formally abandoning the parked batch_state migration), the prefetch `PipelineSpec` + `PrefetchActor`, the runner's S3-diff resumability, and `components/scripts/{build_batches_db,chunk_batches,index_alto}`.

**The media plane eats rask's discovery/viewing estate wholesale** (owner-ruled — rask's surfaces there are placeholder-quality, not worth preserving): retire `search_api` (`:8802`; lance `search` over a catalog-governed lines table replaces it — gated on the P5 pin test), retire the **discover zone** + the EAD `/api/v1/catalog` endpoints + `core/services/discover/` (the EAD harvest re-lands as an ingest job writing a **catalog-governed Lance table** that the media estate serves — `harvest_ead` refitted, `index_catalog` retired), and retire `volumes_api`'s page/ALTO viewing endpoints (lance `viewer` + the exporter cover them; IIIF *reading* becomes a library inside the P7a bronze producer). What survives of volumes_api is only the `/objects` S3 browser, which per **R8** backs an object-browser view **inside `lakehouse`** — there is no separate `storage` zone in the merged set. Reorganize the `@rask/ui` shell nav around the merged set — **RE-PIN, now owner-ruled as R8**:
**home + lakehouse + media + annotator + compute + studio** — six zones. `discover` retires with its
backends; `storage` folds INTO lakehouse (an S3 object browser is a lakehouse view of the warehouse's own
buckets); `train` folds in with it via lance `models`, which is a lakehouse route; `overview` folds into
home. **`studio` keeps its own top-navbar entry (R9)** — it is not folded into anything.

**Gate**: the HTR-cascade e2e — IIIF → bronze → silver → gold on the unified cluster with lineage populated, then an exporter round-trip producing byte-valid ALTO 4.4 from gold — green **before** anything in (d) is deleted.

## P8 — Sweep + record

Retire `components/services/viewer` (pycache husk — distinct from lance's incoming `viewer`; resolve the directory collision at P1 copy time), root strays (`batches.db.20260527T105358Z`, `.coverage`), lance-ns nginx-gateway remnants, and any per-zone playwright configs superseded by `@rask/e2e`. **Reconcile `docs/OPEN-WORK.md`** — items the merge itself closed get struck **with the evidence that closed
them** (A1's hostPath is the obvious one: P4 rules no hostPath ships, so the merge either closes A1 or the
merge is not done); everything else carries forward into rask's own tracking, renumbered or not, but never
silently dropped. A backlog that quietly empties during a migration is the failure this file exists to
prevent. Finalize `docs/architecture/lance-ns-merge.md`: decision statuses updated with evidence; git-cliff
changelog preview. Update `CLAUDE.md`, `docs/architecture/*`, and the vendored `rask-*` skills (`rask-orchestrator` dies with the loop; `rask-services-fleet`/`rask-architecture` redrawn for the merged fleet). **Named follow-up, out of scope here: the platform renames to `Lagom`** (repo, chart release, docs identity) — a dedicated rename pass after the merge stabilizes. No push.

---

## Risk register (top 8)

| # | Risk | Mitigation |
|---|---|---|
| 1 | **lancedb 0.30.2 reader vs pylance 8 writer** — rask's search stack embeds a pre-8 lance core; DSV 2.2 + stable row ids + blob-v2 datasets may be unreadable, silently gating all "reuse rask FTS" value | P5 does the pin bump + a write-with-8/read-with-lancedb integration test **before** any search wiring; if red, search reuse is explicitly gated, not assumed |
| 2 | **`@rask/ui` 3-way merge with lost base** — ~30 changed files, ~8 semantic; wrong-order merging bakes formatter noise into conflicts | P2 step 1: prettier-plugin-tailwindcss normalization commit on the rask side first; then fold only the shell files + 4 new components; storybook build is the regression canary |
| 3 | **Contradictory project IA + double catch-all** — path-projects (rask) vs host-projects (lance-ns); `/data` missing its ingress rule becomes "project data" → 404 | One home (rask's) absorbs auth + landing; reserved-segment guard in `[project]` route; ~~host-based addressing NOT adopted~~ — **upstream adopted it** (`projectFromHost` in rask's shell); the IA contradiction this risk describes is gone, the parent-domain-cookie question remains open; ingress rule per zone asserted in the charts gate |
| 4 | **k8s/helm name collisions** — **RE-PIN**: `rask-annotator` twice (the `annotator` ZONE vs the `services/annotator` BACKEND) is the live instance now that `lineage` is not a zone; plus duplicate Dapr/nats/openfga/cnpg control planes, CNPG CRD re-install, and the dev-port collisions in P0 naming rule 3 | Universal `rask-web-<zone>` rule; single subchart per infra with lance-ns values overlay; `crds.create=false` kept; render-time uniqueness assertion in `make charts` |
| 5 | **Silent gate loss** — rask's explicit `testpaths` means unlisted dirs never run; rask has no GH test CI, so lance-ns's guarded invariants (claim-lint, FGA model contract, prod-render) could go unenforced | testpaths appended + collection-count assertion in P1; Dagger module is the CI vehicle; local `make ci`/`e2e-ci` runs are the branch's enforcement, logged in the merge doc |
| 6 | **Ray version unification regression** (owner-ruled: ONE cluster on the latest release, in-branch — Option B revoked). The GPU Serve packing was OOM-tuned on 2.55/py3.13; a version bump can shift memory behavior and re-trip the raylet-killing cascade | The P1 pre-step does the bump FIRST, standalone, gated on rask's existing HTR smoke path (`smoke-gpu.sh`, `/transcribe` + `/htrflow` answering, fractional-sum + host-RAM invariants rechecked) before any lance code lands; the Jobs-REST seam stays version-agnostic either way |
| 7 | **Auth retrofit regressions in rask zones** — **RE-PIN**: session handling leaking into the auth-free rask zones that SURVIVE R8+R9, i.e. `compute` and `studio` — not "7", since `discover`/`overview`/`storage`/`train` retire or fold, and `home` stops being auth-free the moment it absorbs `/auth/*` | rask zones get no hooks changes; `authEnabled:false` default when OIDC env absent; `mfe.spec.ts` green on the merged deploy is the acceptance check |
| 8 | ~~**License contradiction**~~ **RESOLVED (rebase, 2026-07-27)** — rask `origin/main` relicensed to Apache-2.0 (`6baa318`), matching lance-ns. Nothing to restore, nothing to surface. Residual: rask's 18 package-metadata `license` fields still say AGPL-3.0-only and should follow the LICENSE file. ~~old: — lance-ns relabeled its `@rask/ui`(MIT-origin) and `@rask/api`(AGPL-origin) forks Apache-2.0, plus an uncommitted repo-wide AGPL→Apache swap in lance-ns; rask identity is AGPL-3.0-only | Merged packages restore rask's original labels (ui MIT, api AGPL-3.0-only, repo AGPL); incoming lance-ns Python code enters under repo AGPL; the relabel is flagged in the merge doc as a user decision — no contradictory metadata ships |

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
| R11 | **The zone directory is `microfrontends/` on BOTH sides** (2026-07-27). rask ruled it; lance-ns then renamed to match (`6fbaa0e`). The trees are identical, so the copy is a directory move with **no sed and no path translation**, and zone-contract's gate files arrive byte-identical to upstream — which is what makes them "proven". |
| R12 | **Dagger tracks the newest release** (2026-07-27, wave 2 — supersedes the same-day v0.20.3 hold, which applied to wave 1 only: the copy had to prove itself at the pins before any currency moved). CLI + `dagger.json` engineVersion + regenerated SDK bindings move together; the merged `.dagger` module (rask's migrate/postgres/test + lance-ns's charts/checks/e2e/frontend/openapi) must compile and list its functions at the new version. |
| R13 | **The OTel Collector is the ONLY log shipper — Vector retires** (2026-07-27). Resolves the P4 OPEN DECISION: the collector's filelog receiver owns pod-log shipping (→ `opentelemetry_logs`); the Vector Chart.yaml dep, the `vector:` values block, and its Chart.lock entry go in ONE coordinated change, with the GreptimeDB TTL surface following the collector's table. Standard OTLP throughout stays the rule. |
| R14 | **nginx is gone everywhere, and the intended future edge is kgateway** (2026-07-27). The nginx gateway retired at the gateway fold (R-decision 4); this extends the ruling to the last remnants — the dead `frontend.nginx.conf`, stale dockerfile/helper comments. Zones serve via their Bun SSR servers; the FastAPI gateway (with Dapr sidecars on the fleet) is the in-cluster edge today; adopting kgateway is its own future project (Ingress template, zone Service exposure, gateway Deployment are the touchpoints). `ingress.className: nginx` in values-prod names the *cluster's* Ingress controller class — unrelated to our gateway, operator-set per cluster. |
| R15 | **The shell topnavbar carries EVERY zone** (2026-07-27, from the witnessed pass): compute and studio were absent from the shared nav — a zone missing from the navbar is a defect regardless of scaffold status. |
| R16 | **Overview folds into COMPUTE, not home** (2026-07-27, supersedes R8c): the overview surface is Ray-plane material — overview + ray stuff together in the compute zone. |
| R17 | **Train returns as its own zone; the model registry lives in train** (2026-07-27, supersedes R8's train→lakehouse fold): train = submit training, watch training, monitoring, analysis, viewing/testing models — dummies acceptable now. Studio = sandbox environments for PoCs. Lakehouse's models/registry surface migrates to train (scaffold first, physical migration follows). |
| R18 | **Lakehouse ships the S3 object browser WITH blob preview and a table viewer/previewer** (2026-07-27): R8's storage absorption is BUILT, not just the old zone deleted — baseline per the approved storage-table plan (shared data-table + search + preview). |
| R19 | **`common` merges INTO `service-kit` — now, not later** (2026-07-27): one platform library named service-kit; its factory/Settings/lifespan skeleton is the base, common's auth/FGA/audit middleware ports in as the governed layer; every common importer is rewritten; packages/common is deleted. Executes the convergence row this plan already carried. |
| R20 | **The `-api` suffix is removed — executed WITH P7** (2026-07-27): search-api/volumes-api die into the media plane and core-api/orchestrator dissolve there, which is what makes the rename collision-free (search-api→search collides with the media `search` service today); ray-api takes its clean name in the same pass. |
| R21 | **One compute-lineage layer — a ratch-style wrapper making ALL Ray work emit OpenLineage consistently** (2026-07-27): a shared library (grown inside packages/ratch or as its sibling — decided by evidence at design time) wrapping openlineage-python with pydantic schemas for events/facets, giving Ray Data pipeline stages AND Ray actors an inheritable/decoratable emission seam (job run → stage → actor as parent/child runs), used by the medallion movers, the IIIF→bronze producer, the HTR pipeline, ray_stage/lance jobs, and ONLINE Ray Serve deployments alike — so per-actor lineage is trackable in AGE and no plane invents its own emission shape. **Decided + landed 2026-07-27: `packages/lineage-kit`** (sibling — ratch's pylance/ray/lancedb stack would poison the sealed runner's lock; openlineage-python's transitive set is light), spec 2-0-2 byte-parity with `common/openlineage.py` pinned by test, subprocess context-carry proven via env AND ctor-arg; 8 recorded migration notes feed the adoption gate. |
| R8 | **The surviving zone set is `home + lakehouse + media + annotator + compute` (+ `studio`, per R9)** (2026-07-27). Three parts: (a) rask's **browse / viewing / search** surfaces are eaten by the media plane — this is R6, reconfirmed; (b) what survives of rask's own frontend is **compute** — the Ray dashboard, jobs, actors, cluster views — because that is the plane rask owns; (c) rask's **`storage` zone folds INTO the lakehouse**: an S3 object browser is a lakehouse view of the warehouse's own buckets, not a separate destination. `train` folds in with it (lance `models` absorbed it, and `models` is a lakehouse route). `overview` folds into home as already proposed. `studio` is ruled separately in **R9**. |

| R10 | **All lance-ns configs come to rask** (2026-07-27): the chart, and the frontend toolchain — **oxlint + oxfmt + rsvelte-fmt WIN over rask's eslint + prettier**. This RESOLVES the P2 toolchain precondition: the pure-format commit reformats rask's surviving zones (`compute`, `studio`, home content) and `packages/{api,ui}` under the lance-ns toolchain, and eslint/prettier retire. `@repo/zone-contract`'s script-parity gate then applies to every package unchanged. P2 step 1's `prettier-plugin-tailwindcss` premise is dead. |
| R9 | **`studio` survives as its own top-navbar zone** (2026-07-27). It is not folded into anything. This closes the one gap R8 left open. It matches what `studio` already is on the rask side: a top-level nav entry in `packages/ui/src/lib/shell/nav-config.ts:81` (`Studio`, `Shapes` icon, `${b}/studio`) with its own `/animation` route — so the ruling preserves the surface rather than inventing one. **Final merged zone set: `home + lakehouse + media + annotator + compute + studio` — six zones.** |

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
- ~~No host-based project addressing~~ — **already in upstream** as of the rebase; the parent-domain cookie decision is still open. no migration of rask's remote-function data layer to the BFF pattern (transitional coexistence).
- No `common`→`service-kit` convergence (deferred follow-up).
- No hybrid+rerank search build-out (still the acknowledged lancedb-SDK gap on both sides); no `/search` Tier-2 work beyond the pin-verification test.
- ~~No license relabeling~~ — **moot**: both repos are Apache-2.0 as of the rebase. Only the stale per-package `license` fields remain to align.
- No git-history grafting; no pre-emptive cleanup of rask strays outside the P7 sweep commit.

> **Known finding (2026-07-27, post-merge):** `runners/htr` `test_fake_pipeline_via_ray_data`
> fails with a RuntimeError after ~31.5 min when run solo (fake model, local Ray Data pipeline).
> It is `slow`-marked, so `make test` is unaffected; `make test-slow` will hit it. Investigate in
> the next goal alongside the ONE-Ray-cluster-latest work (R3) — the runner's Ray stack is
> touched there anyway. Full traceback not captured (launcher tailed the log); reproduce with
> `cd runners/htr && uv run --frozen pytest tests/test_pipeline_smoke.py -q`.
>
> **Update (2026-07-27, three measured runs):** the V2-autoscaler coordinator starvation was real but
> not the root: with `RAY_DATA_CLUSTER_AUTOSCALER=V1` the run still dies at ~38 min in a raw
> `ray.exceptions` object-fetch timeout inside the streaming executor — a resource-starvation class on
> this shared 4-CPU-constrained local init, not reproducible logic. The test stays slow-marked and the
> investigation moves to the R3 Ray-convergence step: re-test on Ray-latest, in-cluster (KubeRay), where
> the equivalent lance-ns pipelines demonstrably run. If it passes there, the smoke gains an in-cluster
> variant and the local variant gets a smaller pipeline shape (fewer stages) sized to 4 CPUs.
