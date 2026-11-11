# Frontend toolchain

**bun + oxlint + rsvelte-fmt.** One linter and one formatter for every file in the workspace. No workspace
package declares `eslint` or `prettier`, and none of their configs exist.

`prettier` is still in the installed tree, pulled in by the formatter itself: `@rsvelte/fmt` depends on
`prettier-plugin-tailwindcss`, which peer-depends on `prettier`. Nothing here configures or invokes it,
and no `package.json` asks for it; audit that claim with
`grep -rn '"\(eslint\|prettier\)' --include=package.json`.

| Surface                                  | Lint                                  | Format                    | Type-check                                    |
| ---------------------------------------- | ------------------------------------- | ------------------------- | --------------------------------------------- |
| `.ts` `.js` `.mjs` (incl. `*.svelte.ts`) | **oxlint**                            | **rsvelte-fmt** (→ oxfmt) | `svelte-check` (TypeScript 6)                 |
| `.json` `.md` `.css` `.html` `.yaml`     | —                                     | **rsvelte-fmt** (→ oxfmt) | —                                             |
| `.svelte`                                | **oxlint** + `@rsvelte/oxlint-plugin` | **rsvelte-fmt**           | `svelte-check` (TypeScript 6)                 |

Every package declares the same three scripts, and the root runs them over every package with
`bun run --filter '*' <task>`:

```jsonc
"lint":      "oxlint .",
"fmt":       "rsvelte-fmt .",
"fmt:check": "rsvelte-fmt --check .",
```

[rsvelte](https://github.com/baseballyama/rsvelte) is a Rust port of the Svelte 5 compiler built for
oxc. `rsvelte-fmt` formats `.svelte` in process and **delegates every other extension to oxfmt**, reading
the same `.oxfmtrc.json`. `@rsvelte/oxlint-plugin` runs rsvelte's Svelte diagnostics as oxlint rules,
in oxlint's single pass.

## Config: one file per tool, at the workspace root

`.oxlintrc.json` and `.oxfmtrc.json` live here and **nowhere else**. Both tools resolve the nearest
config upward from the working directory, so a config inside a package would silently win for that
package (nearest config wins) and nothing would report it. Ignore globs live inside each config.

## The lint rules that are off, and why

`.oxlintrc.json` extends `@rsvelte/oxlint-plugin`'s `recommended.json`, generated from the live rule
catalog. A few of its rules are written for a components-own-their-CSS codebase and are wrong here; each
is named in the config with its reason:

- `no-unused-class-name` and `consistent-selector-style` assume a component's classes are defined in
  its own `<style>` block. Tailwind utilities are not, by design.
- `no-inline-styles` / `require-optimized-style-attribute` — every hit is a runtime-computed geometry
  value. No class can express those.
- `no-unassigned-vars`, in `.svelte` only — oxlint reads the `<script>` and not the markup, so a
  variable assigned by `bind:this={el}` looks unassigned.

## In-app links go through `resolve()`

`svelte/no-navigation-without-resolve` is ON, as an error: every in-app `href` and `goto` is written
`resolve(...)` (`$app/paths`), which adds the base path and fills a route id's parameters, and is
type-checked against the route tree. SvelteKit 3 reads a string that starts with `/` as a route id and one
without it as a pathname (`resolve('auth/login?redirect=…')`). A path that carries a catalog name or id is
written in the route-id form with parameters, `resolve('/catalog/tables/[table]', { table:
encodeURIComponent(id) })`: a name interpolated into a route id is split into segments and one shaped like
a route group is dropped (`resolve_route` in kit 3.1.1), so a namespace named `(staging)` would link to the
list page. `no-navigation-without-base` and `no-goto-without-base` are OFF: they ask for the `base` string
concatenated by hand, which SvelteKit 3 no longer exports, and report every `resolve(...)` href and `goto`
(measured with oxlint 1.79.0: 72 errors over the console with both on), so they cannot hold beside the
resolve rule.
The rule reports every `href` and every `goto` whose argument is not a `resolve(...)` call, a query
(`goto('?…')`), a `URL` and a `goto(<call>)` included (measured with oxlint 1.79.0). A navigation that
is not to a route says why in an
`eslint-disable-next-line svelte/no-navigation-without-resolve -- <reason>` comment, which oxlint
honours in `<script>` and in markup: a query on the current page (`Explorer.svelte`, three), the current
page's own URL with a new tab parameter (`TableDetail.svelte`), the return path read from the OIDC state
(`auth/callback/+page.svelte`), a `blob:` URL (`BlobPreviewSection.svelte`) and an href `resourceHref`
already resolved (`AuditViewer.svelte`). `oxlint --report-unused-disable-directives` calls the five in
`<script>` unused, and so does the Svelte autofixer, whose rule (eslint-plugin-svelte's) does not report
those forms; both are wrong for this gate: rsvelte's port reports them, and removing any one of the five
fails the lint. `@rask/ui` is
exempt: a design system cannot import an app's `$app/paths`, and its root paths are what `resolve()`
gives in a console with no base path.

## Type-checking

`svelte-check` owns `.svelte` and runs on TypeScript 6. `typescript@7` (the native Go compiler) ships no
JavaScript compiler API, and `svelte-check` calls into it, so TypeScript 7 cannot be the `typescript`
dependency while `svelte-check` is in the toolchain, and every file, `.ts` included, is checked by it.

The console's `check` runs `svelte-check` three times. `tsconfig.json` extends `$app/tsconfig` and covers the
app (`src`, `vite.config.ts`), whose `types: ["$app/types"]` and browser libs describe code that runs in
the page. `tsconfig.tools.json` covers what never ships to the browser and runs under Bun or Playwright:
`serve.ts` (the image's server), `playwright.config.ts` and `e2e/**`, with `types: ["bun"]` and `DOM` kept
in `lib` because a spec's `page.evaluate` callback runs in the page. Both carry the same strictness.
`tsconfig.api.json` extends the app's config with `exactOptionalPropertyTypes` for `src/lib/api` alone: the API client is
pure TypeScript, so the Bits UI reason the app keeps it off does not reach it.

`@rsvelte/svelte-check` takes `--tsgo` and would put the whole workspace on TypeScript 7. Re-measure it
on each release; it replaces `svelte-check` once it resolves SvelteKit's generated modules and honours
`compilerOptions.experimental.async` from the `sveltekit()` options in `vite.config.ts`.

## The Arrow patch

`patches/apache-arrow@21.2.0.patch` replaces apache-arrow's compiled null check
(`builder/valid`, a `new Function` over a `switch`) with a closure. The console's content security
policy has no `'unsafe-eval'`, and without the patch every browser-side Arrow encode (the insert-rows and
create-with-rows forms) fails with a CSP violation.

The closure keeps the switch's semantics: NaN is checked apart, and every other null value is compared
by `===` with what its `case` expression (upstream's `valueToCase`) evaluates to, so an array or object
null value never matches, a date's case fails to build as upstream's does, and a string matches the
value its literal has in the sloppy-mode body `new Function` compiles (every escape, a legacy octal one
and a line continuation included). `console/src/lib/arrow-null-values.test.ts` holds it to upstream's
compiled function across every kind of null value. The one difference: the closure evaluates no code, so
a case that is not a literal in the form `valueToString` writes (an object whose own
`Symbol.toPrimitive` answers other text, `0x10` among it, or a string whose quote closes early,
`a" + "b`) throws a SyntaxError at creation, where upstream compiles it and
evaluates it at the first call; the test pins that refusal, which also fails on an unpatched install.
Re-check both on an apache-arrow upgrade: the patch is regenerated from the pristine package (bun's
cache, `~/.bun/install/cache/apache-arrow@<version>@@@1/builder/valid.{js,mjs}`) with `git diff
--no-index`, and the test copies upstream's body.

## The horizon-layout patch

`patches/horizon-layout@1.0.5.patch` gives the dock's tab groups (`dist/TabGroup.svelte`) the tab-list semantics and the
keyboard focus the library leaves out. horizon-layout keeps the keyboard on each group's `tablist` and makes its tabs
`tabindex="-1"`, so the list names its active tab with `aria-activedescendant`, each tab carries an id and
`aria-selected`, and the shown view is a `tabpanel` labelled by its tab: without them a screen reader is told neither
which tab is shown nor what the panel holds. A move or a split re-renders the groups it touches, so after a keyboard
action the patch focuses the list whose active tab is the one that moved; unpatched, focus falls to the page and the
next key does nothing (measured on the deployed workbench with playwright-cli, 2026-11-11).
`e2e/workbench/dock.spec.ts`'s keyboard test presses each key where the keyboard is, and fails on an unpatched install.
Both gaps are reported upstream as https://github.com/play-horizon/horizon-layout/issues/23, and the patch goes once a
release carries the fix.
On a horizon-layout upgrade, drop the patch if upstream ships the same; otherwise regenerate it with
`bun --cwd=frontend patch console/node_modules/horizon-layout`, the edit, and `bun --cwd=frontend patch --commit` on
the same path.

## Knip and SvelteKit's generated `$app`

`console/tsconfig.json` extends `$app/tsconfig`, which `svelte-kit sync` writes; it is no package. Knip 6.45.1 reads
the `extends` as a dependency and reports `$app` unlisted, and `ignoreDependencies` cannot silence it (knip's
package-name parser returns nothing for a `$` specifier, so the ignore list is never consulted), so `knip.json`
ignores the `unlisted` issue type for that one file (`ignoreIssues`).
