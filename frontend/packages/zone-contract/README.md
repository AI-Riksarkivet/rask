# @rask/zone-contract

**Architecture fitness functions for the micro-frontend split.** Not a library — nothing imports it and
it ships zero bytes. It is a set of tests that assert things about the _shape_ of the estate that no
compiler can see, because they are agreements between files written in different languages.

Run: `bunx turbo run test --filter=@rask/zone-contract` — 591 assertions, ~3 seconds.

## Why it exists

Splitting one app into four turns routing from **code** into an **agreement across seven files**:

| Declares                                  | Where                                         | Language   |
| ----------------------------------------- | --------------------------------------------- | ---------- |
| which paths a zone owns, and its dev port | `microfrontends/home/microfrontends.json`     | JSON       |
| the base path it serves its assets under  | `microfrontends/<zone>/svelte.config.js`      | JS         |
| the dev port it actually binds            | `microfrontends/<zone>/vite.config.ts`        | TS         |
| the Ingress route and Service             | `chart/values.yaml` → `frontend.apps`         | YAML       |
| which images to build and side-load       | `Makefile` → `ZONES`                          | Make       |
| which workspaces the builder needs        | `.docker/frontend.dockerfile` → `COPY`        | Dockerfile |
| where a cross-zone link points            | `<a href="/explorer/…">` in three other zones | Svelte     |

TypeScript reads one of those. Turborepo reads none of them. SvelteKit reads one.

The demonstration: change `paths.base` in `svelte.config.js` from `/explorer` to `/search`, and every other
gate passes — `svelte-check`, `tsgo`, `oxlint`, `rsvelte-fmt`, `vite build`, and the Playwright suite
(which drives each zone on its own port and so never sees the composition). The build succeeds and
serves every asset from a path the Ingress does not route: a blank page, shipped green.

This is the same move the repo already makes twice elsewhere — `packages/service-kit/src/service_kit/governed/auth/model.fga.yaml`
asserts what the FGA model actually grants, and `scripts/prod_render_check.sh` asserts the prod Helm
render really has default-deny on. Config spread across files, no compiler, silent failure, so you write
tests. The industry name is an [architecture fitness
function](https://www.thoughtworks.com/en-us/insights/articles/fitness-function-driven-development).

## Layout

One file per concern. Helpers that more than one gate needs live in `workspace.ts` — they used to be
private to `manifest.test.ts`, which is how that file grew to nine unrelated concerns.

| File                           | Asserts                                                                           |
| ------------------------------ | --------------------------------------------------------------------------------- |
| `manifest.ts`                  | _(not a test)_ reads the seven sources above — the shared accessor layer          |
| `workspace.ts`                 | _(not a test)_ filesystem facts: workspace packages, a zone's e2e mock servers    |
| `manifest.test.ts`             | the zone manifest: zones declared everywhere, base paths agree, ports unique      |
| `toolchain.test.ts`            | one linter, one formatter, one config each, invoked identically everywhere        |
| `deploy-path.test.ts`          | turbo task order, `Makefile ZONES`, dockerfile `COPY`, nothing names a dead path  |
| `cross-zone-reload.ts` + test  | a cross-zone `<a>` hard-navigates — on Svelte's own compiler, not a regex         |
| `link-targets.ts` + test       | every domain-relative link lands on a route that exists                           |
| `bff-routes.test.ts`           | every BFF route has a caller, and each zone gets only the upstreams it uses       |
| `live-stream.test.ts`          | a live query survives both idle timeouts                                          |
| `poll-reason.test.ts`          | every deliberate poll states why                                                  |
| `no-networkidle.test.ts`       | no e2e waits on `networkidle`                                                     |
| `notification-surface.test.ts` | the run-notification bell is estate-wide, not lakehouse-only                      |
| `proxy.ts` + test              | _(a runtime, not a test)_ the dev composition edge — one origin, like the Ingress |

## What it does not do

It proves the _declarations_ agree. It does not prove anything **runs**. Three layers, each catching
what the one below cannot:

1. **this package** — the seven files agree (3 s, every `turbo run test`)
2. **the `web-smoke` CI job** — the dockerfile actually builds (~5 min, every push)
3. **kind + `scripts/verify_cross_zone_oidc.sh`** — the pods serve and the session carries across zones

Every gate here was written _after_ the bug it catches. If one starts failing, read its comment before
changing it: the comment names the incident.

## Cost, honestly

1,753 lines and ~3 seconds. Nothing imports it, so no zone bundle grows. It is a workspace member so
`turbo run test` fans out over it and caches it — the same reason every other package is one.

Its source is copied into each zone image's **builder** stage (`COPY frontend/packages packages`), which
cannot be avoided: omitting a workspace member is what makes `bun install --frozen-lockfile` fail with
_"Workspace not found"_, and that broke every zone image build once already. The marginal cost is
~nothing — its devDependencies (`svelte`, `vitest`, `@types/bun`, `@types/node`) are each declared by
2–9 other packages, so they are already in the tree. **No image is built for it**: it has no chart
entry, no Service and no Deployment, and it is absent from `Makefile ZONES`.

Roughly 40% of the gates here (dead links, no vendored blobs, dockerfile↔workspace, no `networkidle`)
would still earn their keep in a single app. The other 60% is the price of having four zones.
