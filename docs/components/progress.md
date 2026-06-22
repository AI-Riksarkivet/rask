# rask frontend → microfrontends: progress

**Goal:** Turborepo + Bun + SvelteKit 2 microfrontends on _our_ stack; shadcn-svelte
grouped sidebar (no double sidebars); port ra-hcp's S3 UI; finish the SSR fix + audit
cleanup; move shared components into the component library (@rask/ui); update docs. Verify
every Svelte edit with the Svelte 5 skill + `svelte` MCP. **Don't break anything** — test
at each gate; not sloppy.

## Conventions (loyal to OUR stack, not the with-svelte example's)

- **Bun** (package.json `workspaces`, no pnpm-workspace.yaml); **svelte-adapter-bun** (not adapter-auto)
- **valibot** (not zod); `trailingComma: "all"`; `prettier-plugin-tailwindcss`
- Apps under `components/apps/*`; packages under `packages/*`; **explicit** workspace membership (no globs)
- SSR via **remote functions**; gateway sits _behind_ the SvelteKit server (`RASK_GATEWAY_URL`)
- **`kit.paths.base`** per MFE app (NOT raw vite `base`)
- Internal package scope: `@rask/*` (@rask/ui renamed to `@rask/ui` (was the placeholder `@your-repo/@rask/ui`))

## Phases

### Phase 0 — setup

- [x] progress.md
- [x] tasks tracked (#1–#7 + sidebar/S3/lib/docs)

### Phase 1 — audit cleanup (do on single app, before split) [SAFE]

- [x] delete dead `i18n.svelte.ts` (verified 0 importers)
- [x] `on:keydown` → `onkeydown` (viewer/[volume]/[page]/+page.svelte)
- [x] add `{#each}` keys: search/+page.svelte (×5), catalog-hit-card.svelte (×4)
- [x] verify: svelte MCP autofixer clean + `check` 0/0 + `lint` (each-key warnings gone)
- [ ] DEFERRED idiom review: viewer/[volume]/[page]/+page.svelte — autofixer flags
      "state assigned inside `$effect`" (catalog/loadPages/savePersisted/showBoxes/…). These
      are legitimate async-load + localStorage-persist + untrack effects; `$derived` can't do
      side-effects/async. Not churning (audit deemed file idiomatic; don't risk viewer breakage).

### Phase 2 — SSR-safe data layer + remote functions

- [x] convert the one fetching load (viewer/[volume]/+page.ts) — thread SvelteKit `fetch`
- [x] api.ts: `listPages(volume, fetchFn=fetch)` so loads pass the provided fetch
- [x] guard `<Toaster>` with `{#if browser}` (svelte-sonner touches browser APIs)
- [x] flip `ssr=true` in +layout.ts
- [x] verify: check 0/0 + build green + **booted Bun SSR server: / →302, 9 routes →200, real SSR HTML, no errors**
- [~] DEFERRED full remote-functions migration (lib/server/env.ts + lib/remote/\*.remote.ts,
  23 endpoints, query/command + valibot). SSR blocker is FIXED via fetch-threading; the
  onMount client fetches (relative, browser) still work. Doing the remote split DURING the
  MFE split avoids double-work (each app owns its data via the shared @rask/api package).

### Phase 3 — shadcn-svelte sidebar (grouped, unified, NO double sidebars) ✅

- [x] install sidebar (`bunx shadcn-svelte add sidebar`) + sidebar CSS vars in app.css
      (reused old rail bg for continuity; check stayed 0/0 after the button/tooltip overwrite)
- [x] `nav-config.ts` groups: **Compute** (overview/cluster/jobs/actors/logs/serve/api),
      **Documents** (search/viewer/browse), **Batches**, **Storage** (S3)
- [x] `app-sidebar.svelte` (collapsible=icon, grouped, isActive, Viewer random-batch action)
- [x] wired root `+layout.svelte` (Sidebar.Provider + AppSidebar + Sidebar.Inset, h-svh)
- [x] **stripped ray-shell's w-14 rail** + replaced topbar home with Sidebar.Trigger (kills double sidebar)
- [x] `/s3` placeholder route (so the Storage link doesn't 404; filled in Phase 4)
- [x] verify: 4 MCP autofixers clean + check 0/0 + build + **SSR run: 12 routes 200/302,
      all 4 group labels render server-side, 0 old rails, exactly 1 sidebar root, no errors**

### Phase 4 — S3 UI ✅ (scaffold demonstrating our patterns)

- [x] finding: ra-hcp buckets UI is for HCP multi-bucket (different backend); rask has only 2 fixed
      buckets (images-batch / -alto) + `storage.iter_keys` (no HTTP browse endpoint). So a literal port
      doesn't fit — built a rask-native S3 browser scaffold instead (the "dummy that shows how we code").
- [x] **first remote function**: `lib/remote/storage.remote.ts` `listObjects` query (valibot picklist args,
      server-side, absolute gateway URL via `lib/server/env.ts` RASK_GATEWAY_URL). Types/const in `lib/storage.ts`
      (remote files may export ONLY remote functions — build enforces this, check does NOT).
- [x] `/s3` browser UI: bucket selector + prefix breadcrumb + folder/object table, graceful
      loading→endpoint-pending states; under the unified sidebar (no double sidebar).
- [x] verify: MCP clean + check 0/0 + build OK (remote-entry emitted) + SSR run 200, single sidebar, no crash
- [ ] TODO (backend, separate): add `GET /api/volumes/objects?bucket=&prefix=` to volumes-api
      (delimiter `list_objects_v2` over `storage.s3_client`) → makes the browser live

### Phase 5 — turborepo (non-breaking) ✅ + MFE split (later)

- [x] turbo 2.9.18 + root turbo.json (build/check/dev; `^build`; outputs build/.svelte-kit/dist)
- [x] root `packageManager: bun@1.3.13`; root scripts delegate `build`/`check`/`dev` → `turbo run`
- [x] verify: `turbo run check --filter=viewer-frontend` → 1 successful (turbo+frontend GREEN).
      NB: full `turbo run check` is red ONLY on component-lib's pre-existing stale Storybook (→ Phase below)
- [ ] LATER (task #10): extract @rask/typescript-config + @rask/eslint-config; split frontend →
      apps under components/apps/\* (compute/documents/batches/storage), each svelte-adapter-bun +
      kit.paths.base, sharing @rask/ui + api/remote; microfrontends.json + `turbo dev` proxy / gateway prod

### Phase 3.5 — component-lib (@rask/ui) Storybook health ✅ [unblocked green turbo + Phase 6]

- [x] bump Storybook 8→10 + `@storybook/svelte-vite` framework (lib, not SvelteKit); vite 6→8; vitest 3→4;
      @sveltejs/package 2.5.8; publint 0.3.21; svelte 5.56; ts 6; dropped 8-era addons (essentials/blocks/test)
- [x] scaffold `.storybook/{main,preview}.ts` (none existed)
- [x] fix stale stories: `context="module"`→`module`; converted button.stories.ts→.svelte (defineMeta,
      sidesteps CSF3+Svelte5 typing); fixed dialog story to the wrapper's real API (no `child` snippet);
      added `src/css.d.ts` for the tokens.css side-effect import
- [x] removed unsupported `package` config from svelte.config.js (@sveltejs/package 2.x); fixed
      tokens.css export to `./dist/` (publint)
- [x] verify: **turbo check 2/2 + turbo build 2/2 (publint All good!) + `build-storybook` succeeds** + MCP clean

### Phase 6 — reusable components → @rask/ui (clean wins done) 🟢

- [x] **badge** promoted to @rask/ui (`@rask/ui/badge`) — 11 frontend sites swapped, local deleted.
      Added `WithElementRef` helpers to @rask/ui utils. **Verified styled**: built CSS contains bg-success/
      bg-warning/rounded-full → the `@source` directive works across the workspace.
- [x] **sort-header** promoted to @rask/ui (`@rask/ui/sort-header`) — 5 sites swapped, local deleted;
      added `lucide-svelte` as an @rask/ui dep.
- [x] **Tailwind @source**: `components/apps/frontend/src/app.css` scans `packages/component-lib/dist`
      (Tailwind 4 ignores node_modules by default — without this, @rask/ui classes render unstyled).
- [x] catalog-hit-card already deleted (dead). @rask/ui exports now: badge/button/card/dialog/sort-header/utils.
- [ ] DEFERRED button/card dedupe: @rask/ui has simpler button (sm/md/lg/icon); frontend's (shadcn "nova")
      has icon-xs/icon-sm used by ray-shell. Needs API reconciliation (promote the richer one) before
      switching — risky, do deliberately, not rushed.
- [ ] DEFERRED: promote the whole sidebar into @rask/ui during the MFE split (every app needs it).

### Phase 8 — the physical 4-app MFE split (REMAINING — large, do deliberately)

Status: **scoped & unblocked, not started.** Everything below is additive; build each new
app OUTSIDE root `workspaces` first, verify it boots in isolation, THEN add to workspaces so a
half-built app never breaks the green monolith.

**Gating finding (2026-06-22):** promoting the **sidebar into @rask/ui cascades to the whole shadcn
ui set** — `ui/sidebar/*` (26 files) depends on `tooltip`, `sheet`, `skeleton`, `input`,
`separator`, `button`, the `hooks/is-mobile.svelte.ts` hook, and `$lib/utils` (cn + WithElementRef,
already in @rask/ui). So "shared sidebar" = move the entire ui primitive layer to @rask/ui. Do this as a
focused move (one PR), each set verified with `@source` (see [[reference-rask-ui-tailwind-source]]).

**Order:**

1. Move the shadcn ui set → @rask/ui (`tooltip`/`sheet`/`skeleton`/`input`/`separator`/`button` dedupe +
   `sidebar` + `is-mobile`). Reconcile button (frontend "nova" icon-xs/icon-sm vs @rask/ui sm/md/lg/icon).
   Put `app-sidebar`/`nav-config`/`ray-shell` in a shared `@rask/app-shell` (or @rask/ui) too.
2. Extract `@rask/typescript-config` + `@rask/eslint-config` (with-svelte structure) — created WITH
   the new apps (each app's tsconfig/eslint extends them from day one; do NOT rewire the monolith).
3. Scaffold apps under `components/apps/{compute,documents,batches,storage}-frontend`, each:
   `svelte-adapter-bun`, `kit.paths.base: '/<group>'`, `experimental.remoteFunctions`, own
   `app.html`/`app.css` (with `@source` @rask/ui), shares @rask/ui + a shared `@rask/api` (the api.ts +
   remote/ + server/env.ts, moved out of the monolith), the shared sidebar.
4. `microfrontends.json` (root app = the catch-all) + `turbo dev` proxy (:3024) for local compose.
5. Prod routing: gateway/K8s ingress path-routes `/compute`, `/documents`, `/batches`, `/storage`
   to each app's Bun server (mirrors the per-domain backend-service pattern). **Topology call needed.**
6. Retire the monolith routes as each app takes over; keep `make viewer` until parity.

### Phase 7 — docs (ongoing)

- [x] CLAUDE.md: frontend now SSR/svelte-adapter-bun + grouped sidebar + MFE-bound; @rask/ui=@rask/ui
      Storybook 10; added Turborepo + SSR-Svelte5 conventions; gateway-behind-SvelteKit clarified
- [x] `docs/architecture/frontend-microfrontends.md` (decomposition + component audit + exec order)
- [ ] update when the MFE split actually lands (per-app ports, microfrontends.json, gateway routing)

## Decisions log

- MFE decomposition mirrors the sidebar groups + backend domains.
- Each MFE = its own svelte-adapter-bun SSR server; composed by `turbo dev` proxy (dev) +
  gateway/K8s ingress (prod). Turbo's MFE proxy is **dev-only**.

## Status log

- 2026-06-18 frontend bumped to npm-latest + svelte-adapter-bun; check 0/0, lint green, build emits Bun server.
- 2026-06-18 audit complete (6 agents, MCP-verified): frontend already idiomatic Svelte 5; finite fix list.
- 2026-06-18 Phase 1 cleanup done: deleted dead i18n.svelte.ts; onkeydown; 9 {#each} keys. check 0/0, MCP clean.
- 2026-06-18 Phase 2 SSR fixed: fetch-threading in load + api.ts; ssr=true; Bun SSR server runs (12 routes 200/302).
- 2026-06-18 Phase 3 sidebar done: shadcn grouped sidebar (Compute/Documents/Batches/Storage); ray-shell rail
  stripped → single sidebar; SSR-verified (4 group labels render, 0 old rails, 1 sidebar root).
- 2026-06-18 Phase 5 turbo done: turbo 2.9.18 + turbo.json; root delegates; frontend check green via turbo.
- 2026-06-18 Phase 3.5 @rask/ui Storybook 8→10 done: framework svelte-vite, config scaffolded, stories fixed;
  turbo check 2/2 + build 2/2 + build-storybook OK. Whole workspace green.
- 2026-06-18 docs updated (CLAUDE.md + frontend-microfrontends.md). FULL VERIFY PASS: turbo check 2/2,
  build 2/2, lint green, SSR run = 12 routes 200/302, single sidebar on every page, no server errors.
- 2026-06-22 Phase 4 S3 scaffold + Phase 6 reusable components (badge+sort-header → @rask/ui, verified styled).
- 2026-06-22 **LOCKED IN (user choice "finish what's green")**: Phases 1–6 + docs complete & verified —
  turbo check 3/3, build 2/2, lint 0, SSR 12/12 routes, 0 errors. Phase 8 (4-app split) + OpenAPI codegen
  DEFERRED to a deliberate later effort (scoped above). Nothing left half-built; monolith fully works.
- 2026-06-22 MFE split STARTED + pushed to main: `@rask/ui` rename (was placeholder `@your-repo/oxen`);
  first microfrontend `components/apps/storage-frontend` (svelte-adapter-bun, kit.paths.base /storage,
  shares @rask/ui). Repo cleanup (stray .venv removed, projects/viewer deleted, .gitignore hardened).
- 2026-06-22 Both frontends now DEPLOYABLE as Bun-server images (verified in Docker — non-root, tini PID 1):
  NEW `.docker/storage-frontend.dockerfile` (/storage → 200 in-container) + FIXED `.docker/frontend.dockerfile`
  (was nginx-static — BROKEN by the SSR migration — now a Bun server; /batches → 200, sidebar renders).
  Gotchas encoded: adapter externalizes @sveltejs/kit (ship node_modules); bun-1.3 isolated linker
  (preserve .bun store + symlinked node_modules, run from app dir). Images ~850MB — slimming deferred.
  REMAINING: microfrontends.json + turbo dev proxy; the other 3 domain apps; sidebar→@rask/ui; gateway routing.
