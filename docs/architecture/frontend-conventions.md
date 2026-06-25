# Frontend conventions (the canon)

This is the **single source of truth** for how rask's frontend is written. The
codebase is checked against it, and every future change is reconciled to it —
so the same inconsistencies stop being rediscovered.

Scope: the 7 SvelteKit microfrontends — the 6 domain apps under
`components/apps/*-frontend` plus the catch-all `components/apps/frontend`
(package `viewer-frontend`) that owns `/` (the platform home) — the shared
`@rask/ui` design system, and the `@rask/api` data client. It assumes the architecture in
`frontend-microfrontends.md` and `frontend-monorepo.md` — read those for the
_why_; this doc is the _rules_.

**How to read each concern.** Every section gives:

- **THE canonical pattern** — one copy-pasteable shape. There is exactly one.
- **Reject** — anti-patterns. If you see them, fix them.
- **Gate** — `knip` / `eslint` / `svelte-check` / `tsc(strict)` enforce it in
  `make check` + `turbo run check`, **or** `convention` (reviewer-enforced, no
  machine gate yet).

rask makes **deliberate** choices that override the generic Svelte/MFE skill
advice (e.g. "start with a monolith", "exactOptionalPropertyTypes everywhere").
Where a rule contradicts a skill, the rask choice wins and is flagged.

---

## 0. The stack, in one breath

7 SvelteKit apps, each **SSR via `svelte-adapter-bun`** (`bun ./build/index.js`),
each served under a static base `/<project>/<domain>` (today `/default/<domain>`),
composed behind the **Turborepo microfrontends proxy** (`microfrontends.json`,
default project port `:3024`). Data is **server-only remote-function `query()`**
that reuses **`@rask/api`** via `getRequestEvent().fetch`. Components come from
**`@rask/ui`** (Bits UI headless, styled there — never restyled in apps).
Styling is **Tailwind 4 + OKLCH `@theme` tokens** from `@rask/ui/styles/tokens.css`

- the `style:` directive. TypeScript is **strict + `noUncheckedIndexedAccess`**
  everywhere; **`exactOptionalPropertyTypes` is OFF on Svelte packages, ON for
  `@rask/api`**. Gates: `knip` + `eslint-plugin-svelte` + `svelte-check` +
  `tsc --strict`, run by `make check`.

---

## 1. Data fetching — `query()` + `.refresh()`, mutations, no ad-hoc `load`

**THE canonical pattern.** Every read is a **server-only remote `query()`** in
`src/lib/remote/<domain>.remote.ts` whose body calls a **`@rask/api`** function,
passing **`getRequestEvent().fetch`**. The `@rask/api` functions fetch **relative
`/api/*`**; a per-app server hook (`src/hooks.server.ts` →
`makeGatewayHandleFetch(env.RASK_GATEWAY_URL)`) rewrites those to the **in-cluster
gateway** during SSR, so a server read goes straight to the gateway instead of
hairpinning out through the external ingress (a relative URL on the server would
otherwise resolve against the incoming request origin). The request-scoped fetch
inherits cookies and inlines the response into the SSR payload (no hydration
refetch, no `onMount` waterfall). Reuse `@rask/api`'s schemas + parse — **zero
duplicated fetch/validation per app**, and the rewrite is single-sourced in
`@rask/api` so a new app only adds the one-line hook. Client-side fetches stay
relative (same-origin, correct).

```ts
// src/lib/remote/overview.remote.ts
import { query, getRequestEvent } from '$app/server';
import { listBatches, rayJobs, type BatchesPayload, type RayJobsPayload } from '@rask/api';

/** Batch inventory + summary tiles. SSR-rendered initial frame. */
export const getBatches = query(async (): Promise<BatchesPayload> => {
	return listBatches(getRequestEvent().fetch);
});

/** Ray jobs — polled every 5s via `.refresh()`. The dashboard proxy never 5xxs. */
export const getRayJobs = query(async (): Promise<RayJobsPayload> => {
	return rayJobs(getRequestEvent().fetch);
});
```

When the endpoint isn't in `@rask/api` yet (e.g. storage's volumes-api), the
query still fetches **relative `/api/*` via `getRequestEvent().fetch`** — the
same `hooks.server.ts` rewrite carries it to the gateway, so there is **no
absolute-URL special case**. The only difference is that the response is parsed
against a **local valibot schema** instead of an imported `@rask/api` one
(parse-don't-validate at the boundary), and the real upstream status is surfaced
with `error(502, …)` (a plain throw becomes a generic "Internal Error"):

```ts
const res = await getRequestEvent().fetch(`/api/volumes/objects?${params}`);
if (!res.ok) error(502, `volumes-api → HTTP ${res.status} ${res.statusText}`);
return v.parse(S3ListingSchema, await res.json());
```

**Consuming a query** — cache the handle once, then choose by liveness:

- **Initial / SSR-rendered reads**: `const q = getBatches();` then
  `const data = $derived(await q);`. `await` is legal at the top level of an
  async-mode component (`compilerOptions.experimental.async`); the first paint
  already has the data.
- **Live / polled reads**: the **same** query object, read imperatively via
  `q.current ?? null`, refreshed on an interval:

  ```ts
  onMount(() => {
  	const timer = setInterval(() => {
  		rayJobsQuery.refresh().catch(() => {}); // .catch() MANDATORY
  		rayClusterQuery.refresh().catch(() => {});
  	}, 5000);
  	return () => clearInterval(timer);
  });
  ```

  `.catch(() => {})` is **not optional**: one uncaught refresh rejection (a 500)
  evicts the query from cache and kills the poll loop.

**Mutations** are `@rask/api` calls invoked directly from a handler (NOT remote
queries), followed by `.refresh()` of the affected query handles — flicker-free,
no full reload:

```ts
async function runSync() {
	await syncBatches(); // mutation
	await Promise.all([batchesQuery.refresh(), chunksQuery.refresh()]); // re-read in place
}
```

**Reject:**

- A classic `load`/`+page.ts` that re-fetches what a `query()` already provides;
  a bare `onMount(async () => fetch('/api/…'))` waterfall.
- Calling `@rask/api` with the **global** `fetch` inside a query — no origin on
  the server during SSR; always thread `getRequestEvent().fetch`.
- Re-declaring fetch + valibot schemas per app instead of importing them from
  `@rask/api`.
- `setInterval(() => q.refresh(), …)` **without** `.catch()`.
- Re-calling the query function to "reload" (`getBatches()` again) instead of
  `.refresh()`ing the existing handle. `getBatches() === getBatches()` while on
  the page — re-calling defeats the cache.
- `redirect()`/`error()` thrown from a `command()`-style mutation expecting it
  to navigate; mutations return values, the page reacts.

**Why the hook (the SSR-origin problem).** `getRequestEvent().fetch` of a relative
`/api/*` resolves against the browser origin on the client, but a server has no
origin — raw during SSR inside a k3s pod the relative URL fails ("Unable to
connect") or hairpins out through the external ingress. That is what the
`makeGatewayHandleFetch` hook above solves: SvelteKit calls `handleFetch` only for
server-side `event.fetch`, so it rewrites SSR `/api/*` to the in-cluster gateway
and leaves client requests untouched. **All four data apps** (`overview-frontend`,
`discover-frontend`, `compute-frontend`, `storage-frontend`) carry the **identical**
hook in `src/hooks.server.ts` — no per-app variation. The catch-all `frontend` has
**no** `@rask/api` data layer (so no hook).

**Gate:** `*.remote.ts` is a knip entry point (dead remote functions are caught
by `knip`). Server-only safety (no client import of `$app/server`) is enforced
by SvelteKit's build + `svelte-check`. The `getRequestEvent().fetch` discipline
and `.catch()` on poll loops are **convention** (reviewer-enforced).

---

## 2. Reactivity & state — the runes decision tree

**THE decision tree** (pick the first that fits, top-down):

| Need                                    | Use                                   |
| --------------------------------------- | ------------------------------------- |
| A prop                                  | `$props()` destructuring              |
| A two-way-bound prop                    | `$bindable()` (with a default)        |
| A value computed from other state       | `$derived(...)` / `$derived.by(...)`  |
| Mutable local state                     | `$state(...)`                         |
| A genuine side effect (timers, logging) | `$effect(() => { … return cleanup })` |
| A value that never changes              | plain `const`                         |

**`$derived` vs `$effect` is the load-bearing distinction.** Computed values are
**always** `$derived` (lazy, GC-able, no ordering hazards). `$effect` is the
**escape hatch** — only for side effects that touch the world (intervals,
`localStorage`, analytics) and **always** returns a cleanup. The order of
preference for "do something when state changes" is: **event handler →
`$derived` → `$effect`.**

```ts
// THE pattern — derive, don't effect-into-state
const cachedPct = $derived(totalExpected ? (totalCached / totalExpected) * 100 : 0);

const filtered = $derived.by(() => {
	const term = search.trim().toLowerCase();
	return rows.filter((r) => r.id.toLowerCase().includes(term));
});

// $effect ONLY for real side effects, with cleanup
onMount(() => {
	const timer = setInterval(() => q.refresh().catch(() => {}), 5000);
	return () => clearInterval(timer);
});
```

**Keyed `{#each}` is mandatory.** Every list uses a stable unique key so the DOM
is surgically patched, never reordered in place:

```svelte
{#each chunks as c (c.chunk_id)}…{/each}
{#each rayJobs as j (j.submission_id ?? j.job_id)}…{/each}
```

**Reject:**

- `let x = 0; … x++` for reactive state (use `$state(0)`); plain `let` is not
  reactive in Svelte 5.
- `let doubled = $state(0); $effect(() => (doubled = count * 2));` — the #1
  anti-pattern. Use `$derived`.
- `$effect` to sync two linked inputs, to copy query data into form state, or to
  drive imperative DOM (`el.showModal()`); those loop or fight the framework.
- Reassigning a `$derived` value or a bound `bind:value` target imperatively —
  reset the **source** state instead.
- A keyless `{#each}` or index-as-key `(i)` — recent audits caught real
  reorder/re-render bugs from this.
- Reading browser globals (`window`, `document`, `localStorage`) at component
  top level or in `load`; confine them to `onMount`/`$effect`/handlers (see §5).

**Gate:** `eslint-plugin-svelte` — **`svelte/require-each-key` is `error`**
(keyless each fails CI) and **`svelte/no-reactive-reassign` is `error`** (catches
imperative reassignment of reactive state). The `$derived`-not-`$effect` choice
and runes-vs-`let` are **convention** + `svelte-check` (some misuse surfaces as
type/compile errors). Always validate `.svelte` edits with the **`svelte` MCP
autofixer**.

---

## 3. Components — `@rask/ui` (Bits UI), snippets, no app-local styled components

**THE rule: styled components live in `@rask/ui`, not in the apps.** Apps import
from `@rask/ui` subpath exports; they do **not** define their own styled
component library. `@rask/ui` wraps **Bits UI** (headless) and is the only place
visual styling for shared primitives is authored.

```svelte
<script lang="ts">
	import { Card } from '@rask/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { AppShell } from '@rask/ui/shell'; // the one grouped sidebar, zero drift
</script>

<AppShell pathname={page.url.pathname}>
	<Card class="p-4">
		<Badge variant="success">online</Badge>
		<Button size="sm" variant="outline" onclick={runSync}>Sync</Button>
	</Card>
</AppShell>
```

Import from the **subpath** (`@rask/ui/card`, `@rask/ui/badge`, `@rask/ui/shell`,
`@rask/ui/utils`, …) listed in `@rask/ui`'s `exports`, never a deep
`@rask/ui/dist/...` path. The shell (`AppShell` + grouped `AppSidebar` +
`nav-config`) is shared so every app renders the **same** sidebar.

**Snippets, not slots.** Children come through the `children` snippet and render
with `{@render children()}`; named regions are named snippets. `{#snippet x()}` /
`{@render x()}` — never Svelte-4 `<slot>` / `let:`.

```svelte
<script lang="ts">
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

{@render children()}
```

**Error/pending UI uses `svelte:boundary`** around async query subtrees (the
`pending` snippet for the first frame, `failed(error, reset)` for recovery):

```svelte
<svelte:boundary>
	{#snippet pending()}<div class="text-muted-foreground">Loading…</div>{/snippet}
	{#snippet failed(boundaryError, reset)}
		<Card class="border-destructive/40 …">
			<span>{boundaryError instanceof Error ? boundaryError.message : String(boundaryError)}</span>
			<Button size="sm" variant="outline" onclick={reset}>Retry</Button>
		</Card>
	{/snippet}
	<!-- await-ed queries here -->
</svelte:boundary>
```

**Reject:**

- Defining a styled, app-local component that duplicates a `@rask/ui` primitive
  (a second Button/Card/Badge). Extend `@rask/ui` instead, or compose it.
- Hand-rolling accessible primitives (dialog/select/dropdown) from raw elements
  — use the Bits-UI-backed `@rask/ui` component.
- `<slot>`, `let:item`, `let:data` — Svelte 4. Use snippets + `{@render}`.
- Deep-importing `@rask/ui/dist/...`; importing a component the app then
  re-styles to look different from every other app.
- A bare `{@render children}` without the call parentheses.

**Gate:** unused `@rask/ui` exports and dead components are caught by `knip`
(`packages/ui` declares `src/lib/**/index.{ts,js}` entries). Snippet/`{@render}`
correctness is `svelte-check` + the `svelte` MCP autofixer. "No app-local styled
duplicates" is **convention** (reviewer-enforced).

---

## 4. Styling — OKLCH `@theme` tokens, `style:`, custom properties, Tailwind 4

**THE pattern.** Every app's `src/app.css` imports Tailwind 4, the shared OKLCH
token sheet, and `@source`-scans the built `@rask/ui` so its classes are
generated (Tailwind 4 ignores `node_modules`):

```css
@import 'tailwindcss';
@import 'tw-animate-css';
@import '@rask/ui/styles/tokens.css';

/* Tailwind 4 skips node_modules — scan @rask/ui/dist or its classes vanish. */
@source '../../../../packages/ui/dist';
```

Color/spacing tokens are **OKLCH custom properties** defined once in
`@rask/ui/styles/tokens.css` (`:root` light + `.dark`), consumed via Tailwind
semantic utilities (`bg-background`, `text-muted-foreground`, `border`,
`text-primary`, `bg-emerald-500/70`, …). The dark theme is activated by
`ModeWatcher` setting `class="dark"` on `<html>` — without it the shared sidebar
renders unstyled.

**Dynamic values go through the `style:` directive**, never an interpolated
`style="…"` string. JS→CSS handoff for child components uses the component
custom-property form:

```svelte
<!-- dynamic value → style: directive (type-checked, merges cleanly) -->
<div class="h-full bg-sky-500 transition-all" style:width={`${cachedPct}%`}></div>

<!-- pass a JS value into a child's CSS via a custom property -->
<Card --accent={theme.primary}>…</Card>
```

**Reject:**

- A new color/spacing scale in an app's `app.css` instead of adding a token to
  `@rask/ui/styles/tokens.css`. Tokens are shared, single-source.
- Hex/RGB/HSL literals where an OKLCH token exists; off-palette one-off colors.
- `style="width: {pct}%"` interpolation — use `style:width={...}`.
- Forgetting the `@source '../../../../packages/ui/dist'` line (classes render
  unstyled) or the `ModeWatcher`/`.dark` wiring.
- Reaching into a `@rask/ui` component's internals with broad `:global(...)`;
  theme via custom properties instead.

**Gate:** **convention** + Prettier (`prettier-plugin-tailwindcss` orders
classes). There is no machine gate forbidding off-token colors yet — reviewers
enforce token usage and the `@source`/`ModeWatcher` boilerplate.

---

## 5. Routing / structure / SSR — layouts, `+error`, browser-global safety, base paths

**THE root layout** wires the shared shell, the theme watcher, and same-origin
View Transitions, and renders `children` via a typed snippet. SSR is **on**
(`+layout.ts`: `export const ssr = true; export const prerender = false;`).

```svelte
<script lang="ts">
	import '../app.css';
	import { browser } from '$app/environment';
	import { onNavigate } from '$app/navigation';
	import { page } from '$app/state';
	import { ModeWatcher } from 'mode-watcher';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();

	// Soft client-side nav animation. onNavigate is SSR-safe; guard the API.
	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});
</script>

<ModeWatcher defaultMode="dark" />
{#if browser}<Toaster />{/if}
<AppShell pathname={page.url.pathname}>{@render children()}</AppShell>
```

**`+error.svelte` renders the shared `AppError`** off `$app/state`'s `page`:

```svelte
<script lang="ts">
	import { page } from '$app/state';
	import { AppError } from '@rask/ui/shell';
</script>

<AppError status={page.status} message={page.error?.message} />
```

**Browser-global safety (SSR).** `window` / `document` / `localStorage` must
never run at component top level or in `load` — `$effect` doesn't run during SSR
either. Confine them to `onMount`, `$effect`, event handlers, or guard with
`browser` from `$app/environment` (as the `{#if browser}<Toaster />` above).

**Base paths.** Each app's `svelte.config.js` sets a **static** base
`paths.base = '/<project>/<domain>'` (today `/default/<domain>`). In-app links
use `base` from `$app/paths`; the project segment is derived from it
(`base.split('/')[1] ?? 'default'`). The static base gives the proxy a stable
per-app asset prefix.

**Reject:**

- `export const ssr = false` to dodge a hydration crash; fix the browser-global
  leak instead.
- `window`/`document`/`localStorage` at the top of a `<script>` or in `load`.
- Hardcoding `/default/...` in links; use `base` (and derive `project` from it).
- A custom error page that doesn't go through `@rask/ui/shell`'s `AppError`.
- Dynamic/multi-project base paths — deferred **on purpose** (single project
  `default` for now; see `frontend-microfrontends.md`).

**Gate:** SSR/hydration crashes surface at `vite build` (in `turbo run build`)
and `svelte-check`. Base-path correctness and `+error.svelte`/`AppError` usage
are **convention**.

---

## 6. Micro-frontends — zones, shell/host, namespacing, cross-zone links

rask **deliberately keeps routing-based MFE zones** — 7 independent
`svelte-adapter-bun` SSR apps, each at `/default/<domain>`, composed behind the
Turborepo microfrontends proxy. This overrides the generic "start with a
monolith" advice: the split is intentional, owned, and documented in
`frontend-microfrontends.md`. Do not propose collapsing it.

**THE zone contract** (`microfrontends.json`): each app declares a **fixed local
port** and a `routing.paths` prefix; the proxy routes by it. Vite binds that
exact port with `strictPort: true` so a clash fails loudly instead of silently
drifting and breaking routing.

```jsonc
"overview-frontend": {
	"development": { "local": { "port": 5179 } },
	"routing": [{ "paths": ["/default/overview", "/default/overview/:path*"] }]
}
```

```ts
// vite.config.ts — port matches microfrontends.json, strict, /api → gateway
server: {
	port: 5179,
	strictPort: true,
	proxy: { '^/api(/.*)?$': { target: VIEWER_BACKEND, changeOrigin: true } },
}
```

**Shell/host.** The shared `AppShell` from `@rask/ui/shell` is the host chrome
(one grouped sidebar) rendered identically by every app's root layout — zero
drift. An app owns only its content area + its own internal routing.

**Cross-zone links.** Links **within** an app use `base` (soft client-side nav).
Links to **another domain** are project-prefixed absolute paths built from the
derived project segment, and carry **`data-sveltekit-reload`**: they cross a zone
boundary into a route this app's manifest doesn't contain, so they must be a full
document nav — without the attribute, the global `data-sveltekit-preload-data="hover"`
makes the client router attempt a no-op nav first. The hard nav is animated by the
cross-document View Transition in `tokens.css`:

```svelte
const project = base.split('/')[1] ?? 'default';

<!-- in-app: base (soft) -->
<Button onclick={() => goto(`${base}/new`)}>New volume</Button>

<!-- cross-zone: project-prefixed + data-sveltekit-reload (hard nav) -->
<a href={`/${project}/compute/jobs/${encodeURIComponent(j.submission_id)}`} data-sveltekit-reload
	>details</a
>
<a href={`/${project}/discover/viewer/${b.batch_id}`} data-sveltekit-reload>{b.batch_id}</a>
```

The shared `AppSidebar` applies this **conditionally** — only when a link's domain
differs from the current one; a domain's own sub-routes stay soft.

**Namespacing.** Each app's static base **is** its namespace (asset prefix,
route prefix, port). Don't let two apps claim overlapping path prefixes; the
proxy routes longest-prefix by zone.

**Reject:**

- A non-strict / drifting dev port, or a port that disagrees with
  `microfrontends.json` (silently breaks proxy routing).
- Cross-zone navigation via `goto()` of another app's internal route (it isn't
  loaded in this zone); use a project-prefixed `<a href>`.
- A cross-zone `<a href>` **without** `data-sveltekit-reload` — the client router
  attempts a route this zone doesn't have; it falls back to a hard nav, but only
  after a wasted attempt, and the SSR fetch can resolve against the wrong origin.
- Duplicating shell chrome in an app instead of using `@rask/ui/shell`.
- Hardcoded `/default/...` cross-zone links — derive `project` from `base`.
- Proposing to merge the apps back into a monolith (the MFE split is a
  deliberate rask choice).

**Gate:** **convention** + `strictPort` (a port clash fails the dev server, not
CI). Zone routing/namespacing is reviewer-enforced against `microfrontends.json`.

---

## 7. TypeScript — strictness, parse-don't-validate, `satisfies`, no needless casts

**THE strictness baseline.** Every package is `strict: true` +
`noUncheckedIndexedAccess: true` + `moduleResolution: "bundler"`. Index/`.at()`
access yields `T | undefined` and **must** be handled.

**`exactOptionalPropertyTypes` is split — deliberately:**

- **OFF** on Svelte packages (the 7 apps + `@rask/ui`): Bits UI's prop unions are
  "too complex" under it. `noUncheckedIndexedAccess` carries the strictness.
- **ON** for `@rask/api` (pure TS) — it also adds `verbatimModuleSyntax` +
  `isolatedModules`.

Do not "fix" the inconsistency by flipping `exactOptionalPropertyTypes` on for
Svelte packages — it's an upstream Bits UI incompatibility, not an oversight.

**Parse, don't validate, at boundaries — with valibot.** Untrusted input
(HTTP responses, env, params) is parsed **once** at the boundary into a typed,
trusted value; downstream code trusts the type. The schema is the source of
truth (`type X = v.InferOutput<typeof Schema>`), and `@rask/api` owns the shared
schemas so apps don't re-declare them.

```ts
import * as v from 'valibot';
const ListArgs = v.object({ bucket: v.picklist(BUCKETS), prefix: v.optional(v.string(), '') });

export const listObjects = query(ListArgs, async ({ bucket, prefix }) => {
	const res = await fetch(/* … */);
	if (!res.ok) error(502, `… HTTP ${res.status}`);
	return v.parse(S3ListingSchema, await res.json()); // parse at the boundary
});
```

**`satisfies`, not `as`.** Typed configs/lookup tables use `satisfies` (validates
shape, keeps literals); `as const` for frozen tables. `as` is a last resort with
a comment. Narrow `unknown` with valibot or a `value is T` guard — never `any`.
Errors are narrowed before use: `e instanceof Error ? e.message : String(e)`.

**Reject:**

- `any` (lint warns); silencing an error with `as SomeType` instead of parsing
  or guarding.
- A hand-written `type` that parallels a valibot schema and drifts from it.
- Treating `items[0]` / `arr.at(-1)` as definitely defined under
  `noUncheckedIndexedAccess` (handle the `undefined`).
- Re-declaring `@rask/api` fetch + schema logic inside an app.
- Flipping `exactOptionalPropertyTypes` on for a Svelte package "for
  consistency" — it breaks on Bits UI.
- `enum` (use literal unions); needless non-null `!` where a guard reads clearer
  (sparing `!` after a proven check is fine, e.g. `chunkFilter as number` post-guard).

**Gate:** `tsc --strict` + `noUncheckedIndexedAccess` via `svelte-check`
(apps + `@rask/ui`) and `check:tsgo` / `ty`-style checks; `@typescript-eslint`
flags `no-explicit-any` (warn) and `no-unused-vars` (error, `_`-prefix to
ignore). Parse-don't-validate and `satisfies`-over-`as` are **convention**
backed by the type errors they prevent.

---

## 8. The enforcement gates

`make check` = `fmt` + `lint` + `typecheck` + `knip`. Frontend type-checking
(`svelte-check`) runs through `turbo run check` (the `check` script per app:
`svelte-kit sync && svelte-check --tsconfig ./tsconfig.json`). What each gate
owns:

| Gate                                      | Runs via                             | Enforces (in this doc)                                                                                                                                                       |
| ----------------------------------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`eslint` + `eslint-plugin-svelte`**     | `bun run lint` (in `make lint`)      | **`svelte/require-each-key: error`** (§2 keyed each); **`svelte/no-reactive-reassign: error`** (§2 no imperative reassign); `no-explicit-any: warn`; `no-unused-vars: error` |
| **`svelte-check`**                        | `turbo run check`                    | strict types in `.svelte`/`.ts`, SSR/snippet/`{@render}` correctness, `noUncheckedIndexedAccess` (§2, §3, §5, §7)                                                            |
| **`tsc --strict` (per-package tsconfig)** | `svelte-check` / `check:tsgo` / `ty` | the strictness baseline + the `exactOptionalPropertyTypes` split (§7)                                                                                                        |
| **`knip`**                                | `bun run knip` (root, `make knip`)   | dead remote functions (`*.remote.ts` entries), unused `@rask/ui` exports, dead deps/files (§1, §3)                                                                           |
| **`prettier` (+ tailwind plugin)**        | `bun run format` (in `make fmt`)     | tabs, single quotes, `printWidth: 100`, Tailwind class order (§4)                                                                                                            |
| **`strictPort` (dev)**                    | `vite dev`                           | per-app port matches `microfrontends.json` — clash fails loudly (§6)                                                                                                         |
| **`svelte` MCP autofixer**                | local, every `.svelte` edit          | Svelte 5 correctness before commit (mandatory, §2, §3)                                                                                                                       |

**Everything else is convention** (reviewer-enforced, no machine gate): the
`getRequestEvent().fetch` data pattern and `.catch()` poll loops (§1),
`$derived`-not-`$effect` choice (§2), no app-local styled duplicates of `@rask/ui`
(§3), OKLCH-token-only colors and the `@source`/`ModeWatcher` boilerplate (§4),
base-path/`AppError` usage (§5), zone routing + cross-zone link shape (§6),
parse-don't-validate and `satisfies`-over-`as` (§7). When a convention rule is
violated repeatedly, the fix is to **add a gate** — not to relax the rule.
