# 0223. The console takes its design system from the ui repository (2026-11-20)

**Status:** accepted; supersedes the package part of [ADR 0201](0201-the-console-holds-its-api-client-and-graph-views.md)
("`@rask/ui` is the one package"). Source: FE-022 (#844), decided by the owner on 2026-11-20 through the question tool
("remember to migrate ui dont forget"), with the row's defaults chosen the same day.

## Context

ADR 0201 kept one JS package beside the console, `@rask/ui` (`frontend/packages/ui`): the design system, the shell every
page renders in, and a Storybook to show the components. The design system now lives in a repository of its own,
`AI-Riksarkivet/ui`, published as `@riksarkivet/ui`: one Svelte 5 + Bits UI + Tailwind 4 library, read from source and
pinned by commit, for any app that wants the same components and look, with `DESIGN.md` as its specification and
`docs/CONSUMING.md` as its contract with an app. Its `docs/ADOPTING-FROM-RASK-UI.md` maps every subpath the console
imported onto it: each has the same names there, except the shell, which is app code, and the library adds themes, an
appearance store with a boot script, and the accessibility fixes its gap table lists.

Keeping both would leave two copies of the same components drifting apart, and the console would be the only app on
the older one.

## Decision

- The console depends on `@riksarkivet/ui` at a full commit sha (`"github:AI-Riksarkivet/ui#<sha>"` in
  `frontend/console/package.json`), never a branch. `frontend/packages/ui`, its Storybook and every step that built it
  are deleted; the frontend workspace has one member, the console.
- The shell (`AppShell`, `AppError`, the sidebar, the account menu, the bell, the breadcrumb) is the console's own
  code, `frontend/console/src/lib/shell`, built from the library's components.
- The console owns one copy of each of the library's peers: `bits-ui` 2.18.1 with the library's patch, and
  horizon-layout 2.0.2, which carries the tab-list semantics and the focus after a keyboard move that the console
  patched into 1.0.5, so that patch is gone.
- Appearance is the library's: `riksarkivetUi` in `vite.config.ts` and `appearance.start` in the root layout, with one
  set of options, `storageKey: 'rask-console-appearance'` and `defaults: { theme: 'ink', mode: 'dark' }`. A person's
  earlier light/dark choice is not read.
- A change the library needs is made in the ui repository and reaches the console by moving the pin; rask carries no
  patch of it.

## Why

- **Cloud native.** The design system is a component named by a pinned reference, owned and released in its own
  repository, as every other shared piece of the platform is; the console bundles no copy of it.
- **GitOps.** What the console renders with is what git declares: one sha in `package.json`, recorded in `bun.lock`
  and installed with `--frozen-lockfile`, so the image and a checkout build the same tree.
- **Zero trust.** The library runs under the console's strict CSP unchanged: its boot script is a same-origin file
  (`script-src 'self'`, no hash), and it evaluates no code at runtime.
- Lance-namespace idiom is not touched: the console reaches the lakehouse through the same doors.

## Consequences

The image build fetches `@riksarkivet/ui` from github.com at the locked commit, so the builder needs network access to
it at install time. The console's look changes to the library's (theme `ink`, dark on a first visit), a clickable
table row opens from a button in its first cell instead of being a button itself, and a select is announced with its
value. The rest of ADR 0201 stands: code with one consumer lives in that consumer. Pinned by the console's Playwright
suite (`e2e/catalog/shell-responsive.spec.ts`, `e2e/workbench/dock.spec.ts`) and the shell's
`src/lib/shell/breadcrumb.test.ts`.
