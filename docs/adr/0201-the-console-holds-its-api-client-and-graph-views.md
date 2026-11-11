# 0201. The console holds its API client and graph views; `@rask/ui` is the one package (2026-11-11)

**Status:** accepted. Source: the owner, 2026-11-11, through the question tool: "Console + UI" for the frontend's package
layout, after the FE-021 assessment laid out five layouts.

## Context

Measured 2026-11-11, on the console with the workbench's dock already in it (ADR 0200):

- **Every frontend package had exactly one consumer.** `@rask/api` (the typed `/api/*` client, the lineage client, the
  live-cursor reader and the generated OpenAPI types; 1,100 hand-written lines, 27 tests) and `@rask/flow` (the Svelte
  Flow binding, 1,100 lines, no tests) were imported by the console alone, and `@rask/ui`'s one subpath on `@rask/flow`,
  `./hierarchy`, was rendered by two console pages. Issue #779 records the same for the clients that left: whatever they
  imported from this repository, `@rask/api` and `@rask/flow` included, becomes their own copy.
- **A manifest per package drifted.** `@xyflow/svelte` was `^1.4.2` in `@rask/flow` and `^1.6.2` elsewhere until FE-021
  aligned it by hand; each package repeated `svelte`, `vitest`, `svelte-check` and `typescript`.
- **`@rask/ui` had a build step every gate paid for.** Its `exports` named `dist/`, so `svelte-package` ran before the
  console could type-check, test, run under `vite dev` or build, in the root scripts, the Makefile's `dev` and the
  image's builder stage.
- **The reference console** keeps one component library beside one app, and keeps it as a package because a second
  product consumes it. Here nothing else consumes `@rask/ui` either, but its Storybook is the design system's own
  surface and it stays a package for that.

## Decision

- **`@rask/api` becomes `frontend/console/src/lib/api/`** and **`@rask/flow` becomes `frontend/console/src/lib/flow/`**,
  imported as `#lib/api/….ts` and `#lib/flow/….ts`. `@rask/ui/hierarchy` moves with the binding it draws on, to
  `#lib/flow/hierarchy/`. The two packages are deleted; `@rask/api`'s 27 tests run in the console's vitest.
- **`@rask/ui` is the one package**, read from source: its `exports` name `src/lib/…`, the console's Vite and
  svelte-check compile it, and `app.css` scans `packages/ui/src`. `svelte-package`, `@sveltejs/package` and the
  package's `dist/` go; Storybook builds from the same source.
- **`gen:types` writes `console/src/lib/api/generated/`**, and the drift checks (`make openapi-check`,
  `dagger call openapi`) diff that path.
- **`tsgo` goes with `@rask/api`.** It checked that package alone. The console's `check` runs `svelte-check` (TypeScript
  6) a third time over `tsconfig.api.json`, which keeps `exactOptionalPropertyTypes` on for `src/lib/api` alone; the
  app's own config keeps it off for Bits UI.

## Considered options

| Option | GitOps | Cloud native | Lance-idiomatic | Zero trust | Outcome |
|---|---|---|---|---|---|
| Keep five packages | meets | meets | n/a | meets | rejected: one consumer each, a manifest each, a build step every gate pays |
| Fold the dock and flow only | meets | meets | n/a | meets | rejected: `@rask/api` keeps one consumer and a manifest |
| **Console + UI** | meets | meets | n/a | meets | **chosen** (owner) |
| Fold `@rask/ui` too | meets | meets | n/a | meets | not chosen: the design system keeps its Storybook as a package of its own |

## Consequences

- **Two workspace members**, the console and `@rask/ui`; nothing builds before `check`, `test`, `dev` or the image's
  console build.
- **`@rask/ui` is compiled with the console's settings**, so a component edit reaches `vite dev` through HMR, and a
  component the console's compiler rejects fails the console's gates.
- **A future second consumer of the API client** takes its own copy (issue #779), or the client becomes a package
  again by its own decision.
