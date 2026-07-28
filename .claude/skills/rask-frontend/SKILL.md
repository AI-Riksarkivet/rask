---
name: rask-frontend
description: The rask `frontend/` plane — seven SvelteKit 2 + Svelte 5 zones composed by Turborepo's microfrontend proxy, three data-fetching dialects, and the oxlint/oxfmt/zone-contract gates. Use when touching a zone, `@rask/api`, `@rask/zone-contract`, `microfrontends.json`, or any `.svelte`; when adding a route or fetching data; when a cross-zone link 404s or an SSR fetch hairpins; or when adding a zone or a frontend dependency.
---

# rask frontend

Every JS/TS file lives under `frontend/`, its own bun 1.3.14 + Turborepo 2.9.18 workspace root (`package.json`, `bun.lock`, `turbo.json`, `knip.json`, `.oxlintrc.json`, `.oxfmtrc.json`, `patches/`, `assets/`). Invoke everything scoped: `bun --cwd=frontend run <task>`. The `--cwd=` form matters — `bun --cwd <path>` with a space silently no-ops.

Workspace membership is **globbed** (`microfrontends/*`, `packages/*`), so a directory carrying a `package.json` is enrolled automatically and one without it is **silently skipped** — bun prints "Done!" and the package is never installed, built, or linted.

Styling and component authoring live in **`rask-styling`**. Svelte 5 and SvelteKit idioms live in `svelte-skills:*` and the `svelte` MCP. This skill is the plane above them: zones, data, composition, gates.

## The seven zones

Package name equals directory name for all seven (`manifest.test.ts:53`). Base is a bare `/<zone>` — **no `/default/` segment exists**, and `cross-zone-reload.test.ts:38` asserts `/default/lakehouse` is not a zone path.

| zone | base | dev port | nav label | what it is |
|---|---|---|---|---|
| `home` | `''` catch-all | 5273 | Home | Project gallery + the **OIDC BFF** (`/auth/{login,callback,logout}`) |
| `lakehouse` | `/lakehouse` | 5174 | Lakehouse | The big one — `data`, `lineage`, `models`, `admin`, `storage`; 84 route files, **51 `+server.ts` BFF routes** |
| `media` | `/media` | 5173 | **Search** | Corpus search workbench: FTS/vector/hybrid, WebGPU atlas, Cypher KG, Svelte-Flow editor |
| `annotator` | `/annotator` | 5177 | **Annotate** | One page: PixiJS/WebGPU canvas over Arrow-backed rows |
| `compute` | `/compute` | 5175 | Compute | Ray/Serve observability, 9 pages |
| `train` | `/train` | 5178 | Train | **Placeholder data only** — every page badges it |
| `studio` | `/studio` | 5176 | Studio | Mini-app launcher, one tenant |

Nav labels decouple from directory names on purpose — "named for what it is FOR, not the directory it lives in" (`nav-config.ts:301-303`).

## The seven packages

Only `@rask/ui` has a build (`svelte-package` → `dist/`); the rest are consumed JIT as raw TS.

| package | what it is |
|---|---|
| `@rask/ui` | Design system + `@rask/ui/shell`. → **`rask-styling`** |
| `@rask/api` | Gateway client (`ray`, `ingest`, `projects`, `me`) **plus** the OIDC/BFF plane (`bff.ts`, `oidc.ts`) and the lineage client |
| `@rask/media-api` | Arrow-backed media/viewer client |
| `@rask/engine` | Framework-agnostic PixiJS/WebGPU annotation canvas (ra-anno lineage) |
| `@rask/labeling` | The `LabelOp` model + annotator Arrow-IPC transport |
| `@rask/zone-contract` | **Test-only** — 12 files / 699 tests gating the estate's shape |
| `@rask/config` | One shared `tsconfig.base.json`, extended by 6 of 14 packages |

## Fetching data — three dialects, one per zone family

`experimental.remoteFunctions: true` is set in all seven `svelte.config.js`, and used for reads in **two**. Match the zone you are in rather than converging them.

**(a) Remote `query()` — `compute` and `home`.** A `.remote.ts` query calls a `@rask/api` function through `getRequestEvent().fetch`. On every polled refresh, `.refresh().catch(() => {})` is **mandatory**: one uncaught rejection evicts the query from cache and silently kills the poll loop (`compute/src/lib/remote/compute.remote.ts:25-40`).

**(b) Same-origin BFF — `lakehouse`, `media`, `annotator`.** A per-zone `+server.ts` proxy plus `createBffClient(base)` called from `$effect`. Correct for these zones: they reach services the gateway does not front.

**(c) None — `studio`, `train`.** Hardcoded arrays.

Estate-wide: `command()` 0, `form()` 0, `query.batch()` 0, `{#await}` 0. Every zone opens exactly one `query.live` for the notification bell, always inside `onMount` — opening it at init made the server hold the page.

### The SSR hairpin

Under `svelte-adapter-bun` a relative `/api/*` resolves against the **incoming external origin**, so a server-side fetch leaves the cluster and comes back. `makeGatewayHandleFetch` (`packages/api/src/gateway.ts:33-44`) rewrites `origin + '/api/'` → `gatewayBase + path` during SSR.

⚠️ The two wirings disagree on the env var. `compute/src/hooks.server.ts:11` reads **`RASK_GATEWAY_URL`**; `home`/`lakehouse` go through `makeZoneHooks(env, {gateway:true})`, which reads **`LANCE_GATEWAY_URL`** and defaults to `http://localhost:8001` (`bff.ts:241,267`) — the lineage port, not the gateway. Local dev sets only `RASK_GATEWAY_URL`. Treat a "works in `compute`, fails in `lakehouse`" SSR fetch as this.

## Composition — dev and prod share only the base path

**Dev.** `make dev-frontends` builds `@rask/ui` + `@rask/api` first, then runs `turbo run dev --filter='./microfrontends/*'`. That filter is load-bearing: an unfiltered `turbo run dev` also starts `@rask/ui`'s `svelte-package -w`, which rewrites `dist/` while zones read it, and turbo tears the run down.

Turborepo 2.9.18 has a **built-in** microfrontends proxy. It reads `microfrontends/home/microfrontends.json` and binds `:3024`. `@vercel/microfrontends` is not installed and is not needed. Flow: `browser → :3024 → longest-prefix match → 127.0.0.1:517x (vite, strictPort) → SvelteKit with paths.base=/<zone>`. No path stripping.

> A second, hand-rolled proxy sits at `packages/zone-contract/src/proxy.ts` (`PROXY_PORT ?? 5200`). Its `dev:proxy` turbo task is invoked by no root script and no Makefile target, and its claim that `bun run dev` starts it is false. `:3024` is the live dev origin; `:5200` survives only because `media`'s e2e defaults to it.

**Prod.** One Ingress per release, rules specific-first: `/api` → `rask-gateway:8888`, `/<zone>` → `rask-web-<zone>:3000`, `/` → `rask-web-home:3000` last. `pathType: Prefix`, **no `rewrite-target`** — the pod receives `/compute/jobs` and `paths.base` consumes it. Images are tagged `web-<zone>:<tag>`.

This works only because of `patches/svelte-adapter-bun@1.0.1.patch`: upstream roots sirv at `client/<base>`, but SvelteKit already emits base-prefixed assets *inside* `build/client/`, so `/compute/_app/x.js` resolved to `client/compute/compute/_app/x.js` → 404. Probes are TCP, not httpGet, because a zone's `/` 404s under its base.

## Cross-zone links

A link is cross-zone when `zoneOf(href) !== zoneOf(pathname)`, where `zoneOf(p) = p.split('/').filter(Boolean)[0] ?? ''`. Cross-zone anchors carry **`data-sveltekit-reload`** — without it SvelteKit soft-navigates into a route the zone does not own and 404s. The shell applies this itself (`top-navbar.svelte`); `ZoneNavLeaf.reload` is the sidebar equivalent.

Hrefs are **flat and absolute** (`/lakehouse/data`, `/media/`, `/compute/`) — there is no project prefix. The project comes from the **request host**: `projectFromHost` maps `demo.localhost` → `demo` (`shell/breadcrumb.ts:5-8`).

**Trailing slashes on zone-root hrefs are load-bearing.** Each zone's `paths.base` serves the trailing form, so a bare `/compute` costs a 308 per hop (`tests/nav-config.test.ts:26-29`).

## Adding a zone — five places plus a budget

Globbed membership means there is no list to append to, but five files must agree or the gates fail:

1. `microfrontends/home/microfrontends.json` — port + routing key
2. `svelte.config.js` — `paths.base`
3. `vite.config.ts` — port + `strictPort: true` (and the `dev` script must not also pass `--port`; that race made `annotator` drift onto another zone's port)
4. `chart/values.yaml` `frontend.apps`
5. `Makefile` `ZONES`

Plus a `budget.json` entry. `manifest.test.ts`, `deploy-path.test.ts`, and `budget.test.ts` pin all six. R15 is law: a zone missing from the shared navbar is a defect regardless of scaffold status.

## TypeScript strictness is split

`strict` is on everywhere. `noUncheckedIndexedAccess` is on for the five rask-origin zones + `@rask/ui` + `@rask/api` (hand-inlined), and **off** for `annotator`, `media`, `engine`, `labeling`, `media-api`, `zone-contract` — all six extend `@rask/config/tsconfig.base.json`, which sets neither flag. The shared base is weaker than the inlined copy, so the two lance-imported zones are the least strictly typed in the estate. That is a defect, not a design.

`exactOptionalPropertyTypes` is on only for `@rask/api`; it stays off on Svelte packages for a real upstream reason (Bits UI "union too complex"). Leave that one alone. Validation is **valibot**.

## Gates

ESLint and Prettier are **deleted**. `toolchain.test.ts` enforces three things about every workspace package:

- No `.prettierrc*` / `eslint.config.*` / `.oxlintrc.json` / `.oxfmtrc.json` inside a package — those configs live only at the frontend root.
- No script may match `/\b(eslint|prettier)\b/` — a package spawning a removed tool looks green while checking nothing.
- Every package **declares all three scripts verbatim**: `fmt: 'rsvelte-fmt .'`, `fmt:check: 'rsvelte-fmt --check .'`, and `lint: 'oxlint .'` — or `lint: 'oxlint --no-error-on-unmatched-pattern .'` for a package with no lintable file (`@rask/config` ships two JSON files, where plain `oxlint .` exits 1). The flag is **forbidden** where source exists, so it can never mask a zone whose paths stopped matching. Required, not optional: a package shipping *no* lint/fmt scripts leaves turbo nothing to run and sits silently outside the toolchain while every gate stays green — which is exactly what `@rask/config` did until 2026-07-25.

| Command | Runs |
|---|---|
| `make check` | `fmt` (mutating) + `lint` + Python `uvx ty` + `knip` |
| `bun --cwd=frontend run check test` | svelte-check + the ~960 vitest tests |
| CI (`.dagger/frontend.go:53`) | `bunx turbo run check check:tsgo test lint fmt:check` |

`make check` reaches **neither** svelte-check nor the frontend tests, so run the second row before declaring a change done. `knip` is the inverse — local only, absent from CI.

Lint is **oxlint** (`svelte/require-each-key: error`, `svelte/no-reactive-reassign: error`, via `@rsvelte/oxlint-plugin`); format is **oxfmt** with tabs, single quotes, `printWidth: 100`. The cross-zone link rule is a **vitest test**, not a lint rule — oxlint reads a `.svelte` `<script>` block, not its markup, so an anchor-attribute rule cannot live there.

Validate `.svelte` edits with the `svelte` MCP autofixer. The standing rule that gives it teeth: **a Svelte defect class found twice becomes an oxlint `error` or a zone-contract test.** Three warnings sit unfixed in `compute` today, which is what an unenforced convention looks like.

## Staying on-stack

Animation → **GSAP** via `{@attach}` (+ Lenis). Charts → **LayerChart**. Graph/canvas editors → **Svelte Flow**. Components → `@rask/ui`. Validation → **valibot**. A dependency that duplicates the stack is a no; extend the stack instead.

## Where to go deeper

- `references/zone-map.md` — per-zone routes, endpoints, and libs; the nav/shell contract.
- `rask-styling` — tokens, `@source`, component authoring.
- `rask-services-fleet` — the `/api/*` gateway and the services these zones call.
- `rask-architecture` — workspace planes and membership.
- `docs/architecture/frontend-conventions.md` — the long-form canon. Its `@source` line (`:319`, `:347`) has four `../`; three is correct.
