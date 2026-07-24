---
name: rask-frontend
description: The rask frontend canon — its Svelte 5 + SvelteKit 2 + Bun/Turborepo microfrontend conventions (data via remote query()+refresh, runes, @rask/ui/Bits UI, OKLCH tokens, MFE zones, TS strictness) and the make-check gates. Use when touching any .svelte / a frontend app / @rask/ui / @rask/api / microfrontends.json, doing data-fetching/reactivity/styling/routing, adding a frontend dep or app, or deciding the idiomatic rask way to do a frontend thing.
---

# rask frontend (the MFE canon)

The rask-specific frontend rules — the layer **over** the generic skills below. The exhaustive, gate-mapped canon is **`docs/architecture/frontend-conventions.md`** (8 concerns, each with the pattern + anti-patterns); this skill is the at-a-glance bias + checklist. **Read the canon doc before any non-trivial frontend work.**

## Core skills — read these deeply, every time (not optional)

Frontend work in rask is built ON these. Read them in full (references included), don't skim — this skill is the rask-specific layer over them, NOT a replacement:

- **`svelte-skills:*`** (Svelte 5 + SvelteKit 2 idioms) — `svelte-runes`, `svelte-template-directives`, `sveltekit-structure`, `sveltekit-data-flow`, `sveltekit-remote-functions`, `svelte-components`, `svelte-styling`.
- **the `svelte` MCP** — validate EVERY `.svelte` with the autofixer before done (mandatory), and use it to confirm a pattern against the docs.
- **`micro-frontends`** — the zone / shell-host / resilience theory the rask MFE split is built on (generic; this skill adds the rask config).
- **`turborepo`** — the monorepo build/task/proxy model.
- **`writing-typescript`** — strict TS + parse-don't-validate.

## When to use

- Any `.svelte` edit, a new route/page, or a new MFE app.
- Touching `@rask/ui` (components/shell) or `@rask/api` (the valibot client).
- A data-fetching, reactivity/state, styling, or routing/SSR decision.
- Adding a frontend dependency or skill — bias to the stack below.
- Deciding "what's the idiomatic rask way to do X" on the frontend.

## The stack, one breath

Bun + Turborepo; **7 SvelteKit 2 + Svelte 5 SSR apps** (`svelte-adapter-bun`) as routing-based **MFE zones** at `/default/<domain>` behind the turbo `:3024` proxy (the catch-all `home` app owns `/`); **`@rask/ui`** = the shared design system (Bits UI headless + Tailwind 4 + OKLCH `@theme` tokens); **`@rask/api`** = the valibot fetch client. Deliberately MFE, **not** a monolith.

## The canon at a glance (full detail → the doc)

1. **Data** — server-only remote `query()` reusing `@rask/api` via `getRequestEvent().fetch`; `.refresh().catch()` to poll / after a mutation. NEVER `onMount`/`$effect` fetch waterfalls.
2. **Reactivity** — the runes decision tree; a computed value is ALWAYS `$derived` (never effect-into-state); `{#each}` is ALWAYS keyed by a stable id.
3. **Components** — only `@rask/ui` (Bits UI); snippets + `{@render}`, not slots; zero app-local styled duplicates.
4. **Styling** — OKLCH `@theme` token utilities (no off-palette literals); `style:` / the component custom-property form for dynamic values; Tailwind 4 + the `@source` + `ModeWatcher` boilerplate.
5. **Routing/SSR** — browser globals only in `onMount`/`$effect`/handlers; `+error` per app; cross-zone redirects in `+page.server.ts`; base-relative in-app links.
6. **MFE** — keep the zones; shared `AppShell`/`AppError` (zero drift); cross-zone links project-prefixed + `data-sveltekit-reload`; a new app = two-place workspace membership + `microfrontends.json` + a static base path (workspace layers → `rask-architecture`).
7. **TypeScript** — strict + `noUncheckedIndexedAccess` everywhere (`exactOptionalPropertyTypes` OFF on Svelte pkgs — Bits UI incompat); parse-don't-validate with **valibot** (not zod); `satisfies` over a needless `as`.
8. **Gates** — `make check` = knip + eslint (`require-each-key`, `no-reactive-reassign`) + svelte-check + prettier. A repeated convention violation becomes a NEW gate, not a relaxed rule.

## MFE composition — dev proxy vs prod ingress (the part that bites in deploy)

Two DIFFERENT composition layers stitch the zones; they are NOT meant to mirror each other — they share ONLY the base paths. Confusing them is the usual "ports don't map in k3s" bug.

- **DEV** = turbo's `microfrontends.json` proxy on `:3024` (`turbo dev`). Per-app dev ports (5174…); the application **key** doubles as routing id. `packageName` is OPTIONAL — only needed when a key ≠ its `package.json` `name`; rask's keys ARE the package names, so it's correctly omitted. `options.localProxyPort` just moves the `:3024` proxy (rask uses the default). **All of this is dev-only — turbo says so explicitly; `microfrontends.json` does NOT exist/apply in prod.**
- **PROD** = each app is its own `:3000` container (`svelte-adapter-bun`, `frontend.service.port`) composed by the **k3s Ingress** (the prod reverse-proxy): `/default/<domain>` → that app's Service (pathType `Prefix`, **no strip** — the app keeps its base path), `/` → the catch-all `home`, `/api` → gateway:8888. Source: `chart/templates/{frontends,ingress}.yaml` + `values.frontend`.
- **One parametrized `.docker/frontend.dockerfile`** (`--build-arg APP=…`), built per app in the Makefile loop. **No per-app and no per-package dockerfile** — `@rask/ui`/`@rask/api` are libraries baked into each app image at build time, never their own deployable.
- **The shared dev↔prod contract is the base path** `/default/<domain>` (set in each `svelte.config.js`). Both proxy layers route to it unchanged; nothing else has to match.
- **Deploy gotcha (SSR hairpin):** a `query()` whose `getRequestEvent().fetch` hits a relative `/api/*` works in dev (vite proxy) but in prod resolves against the EXTERNAL ingress host → the pod hairpins out and back. Server-side reads must use the in-cluster `RASK_GATEWAY_URL` when set. Full detail → `docs/architecture/{deployment,frontend-microfrontends}.md`.

## Pre-flight checklist (every frontend change)

- [ ] **Validate every `.svelte` with the `svelte` MCP autofixer** before done (standing rule). And aswell look thoroughly true the svelte 5 skill and dont just skim, actually read it and understand it and check for inconsistnecy with it and the other MFEs.
- [ ] Data via `query()`+`.refresh()` — no `onMount` fetch.
- [ ] `{#each}` keyed; computed = `$derived`.
- [ ] Components from `@rask/ui`; tokens not color literals; `style:` for dynamic values.
- [ ] Browser globals guarded (SSR-safe under `svelte-adapter-bun`).
- [ ] `make check` green (knip + lint + svelte-check) — the gates catch the mechanical rest.

## Adding a frontend lib / dep (stay on-stack)

Animation → **GSAP** (via `{@attach}`) + **Lenis** (smooth scroll); charts → **LayerChart**; components → **Bits UI / `@rask/ui`** (Melt UI if needed); validation → **valibot**. A dep that duplicates the stack or breaks a canon rule is a no — extend the stack, don't fork it.

## Cross-skill

- The **generic foundation** is the "Core skills" section up top (`svelte-skills:*`, the `svelte` MCP, `micro-frontends`, `turborepo`, `writing-typescript`) — read those deeply; this skill is only the **rask-specific** layer + the canon pointer.
- `rask-architecture` (workspace layers / two-place workspace membership), `rask-services-fleet` (the `/api/*` gateway + the per-domain services the frontend queries hit).
