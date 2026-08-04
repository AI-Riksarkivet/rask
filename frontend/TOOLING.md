# Frontend toolchain

**bun + oxlint + rsvelte-fmt.** One linter, one formatter, for every file in the workspace. No workspace
package declares `eslint` or `prettier`, none of their configs remain (`eslint.config.js`,
`.prettierrc*`, `.prettierignore`), nothing in CI, the dockerfiles or the Makefile invokes them, and the
`@rask/eslint-rules` workspace is gone — along with `eslint-config-prettier`, `eslint-plugin-svelte`,
`typescript-eslint`, `@eslint/compat`, `globals` and `prettier-plugin-svelte`.

One honest caveat, because "is prettier gone?" deserves a precise answer: **prettier is still in the
installed tree, pulled in by the replacement formatter itself.** `@rsvelte/fmt` depends on
`prettier-plugin-tailwindcss`, which peer-depends on `prettier`. So the binary exists under
`node_modules/` and appears in `bun.lock` — but nothing in this repo configures or invokes it, and no
package.json asks for it. It is upstream's dependency, not ours; it disappears when rsvelte-fmt drops
it. Audit the question with `grep -rn '"\(eslint\|prettier\)' --include=package.json`, which is the
claim that actually matters, not a bare lockfile grep.

| Surface                                  | Lint                                  | Format                    | Type-check                                |
| ---------------------------------------- | ------------------------------------- | ------------------------- | ----------------------------------------- |
| `.ts` `.js` `.mjs` (incl. `*.svelte.ts`) | **oxlint**                            | **rsvelte-fmt** (→ oxfmt) | `tsgo` (TypeScript 7) on pure-TS packages |
| `.json` `.md` `.css` `.html` `.yaml`     | —                                     | **rsvelte-fmt** (→ oxfmt) | —                                         |
| `.svelte`                                | **oxlint** + `@rsvelte/oxlint-plugin` | **rsvelte-fmt**           | `svelte-check` (TypeScript 6)             |

Every package's scripts are byte-identical, and `@rask/zone-contract` fails if one drifts or if a
package invokes `eslint`/`prettier` again:

```jsonc
"lint":      "oxlint .",
"fmt":       "rsvelte-fmt .",
"fmt:check": "rsvelte-fmt --check .",
```

[rsvelte](https://github.com/baseballyama/rsvelte) is a Rust port of the Svelte 5 compiler built for
oxc. `rsvelte-fmt` formats `.svelte` in process and **delegates every other extension to oxfmt**, so it
is a strict oxfmt superset reading the same `.oxfmtrc.json` — one binary, one config, no second style.
`@rsvelte/oxlint-plugin` runs rsvelte's Svelte diagnostics (the `eslint-plugin-svelte` rule ports plus
the Svelte compiler's own validator and a11y warnings) as oxlint rules, in oxlint's single pass.

## Config: one file per tool, never named by a package

`.oxlintrc.json` and `.oxfmtrc.json` live here and **nowhere else**. Both tools resolve the nearest
config upward from the working directory, so no package needs `--config ../../..` or a per-depth
relative path. Ignore globs live _inside_ each config for the same reason.

This is not theoretical hygiene. `microfrontends/explorer` shipped its own `.oxfmtrc.json` and
`.oxlintrc.json`, inherited from the standalone lance-media repo. They sat dormant while nothing
invoked the oxc tools — and the moment oxlint and oxfmt were switched on they silently won, because
_nearest config wins_. That zone got 80-column double-quoted output against the root's 100-column
tabbed style, a different plugin set, `suspicious: warn`, and two rules disabled. Nothing reported it.
Both files are deleted, and `@rask/zone-contract` fails if a per-package `.oxlintrc.json`,
`.oxfmtrc.json`, `.prettierrc` or `eslint.config.js` reappears — the last two are still on that list
_after_ the tools were removed, because a config for a tool nobody runs reads as coverage that is not
there.

## The lint rules that are off, and why

`.oxlintrc.json` extends `@rsvelte/oxlint-plugin`'s `recommended.json`, which is generated from the
live rule catalog and so never drifts from the engine. A handful of its rules are written for a
components-own-their-CSS codebase and are wrong here. Each is named in the config with its reason
rather than dodged by extending a smaller preset, so a release that fixes one is a visible edit. The
short version:

- `no-unused-class-name` (193 hits) and `consistent-selector-style` (389) both assume a component's
  classes are defined in its own `<style>` block. Tailwind utilities are not, by design.
- `no-navigation-without-base` / `no-goto-without-base` flag exactly what the estate requires: a
  cross-zone `<a>` must **not** carry this zone's base, and `goto('?…')` has no base to carry.
- `no-inline-styles` / `require-optimized-style-attribute` — every hit is a runtime-computed geometry
  value (treemap rects, a split pane's percentages, a progress width). No class can express those.
- `unicorn/no-new-array` — every use is a pre-sized buffer in the Pixi render path, where the
  suggested `Array.from({length: n})` also fills.
- `no-unassigned-vars`, in `.svelte` only — oxlint's `.svelte` support reads the `<script>` and not
  the markup, so a variable assigned by `bind:this={el}` looks unassigned. Wrong on every canvas ref.

## The cross-zone gate moved, and got better

The one rule that was load-bearing rather than stylistic — a cross-zone `<a>` must carry
`data-sveltekit-reload`, or it soft-navigates into a route manifest that does not contain the target
(a 404 that type-checks, unit-tests and renders) — was a local ESLint rule, and was the last real
reason ESLint was installed.

It is now `@rask/zone-contract/src/cross-zone-reload.ts`, running on **Svelte's own compiler**: the
same parser that builds the component, rather than a second AST from a lint plugin. It runs over every
component in every zone as part of `turbo run test`, reports `file:line` and the offending href, and
keeps the original rule's unit tests plus a new one asserting its zone list still equals the real zone
set. Verified by introducing a violation and watching it fail.

## rsvelte-check — not yet, and the reason changed

`@rsvelte/svelte-check` is the piece that would retire `svelte-check` and `typescript@6` and put the
whole workspace on TypeScript 7, because it takes `--tsgo`. Measured at `0.5.1` against this repo:

The previous blocker — it emitted invalid TSX for `data-table.svelte`, and the syntax error made
TypeScript **suppress all semantic diagnostics program-wide** while still exiting green — is **gone**.
It now checks all four zones and reports real errors, including one `implicit any` that `svelte-check`
does not flag.

What remains is a module-resolution gap: on `media` and `annotator` it cannot resolve
`$env/dynamic/private` (23 false errors), even though `svelte-kit sync` generates the declaration and
`svelte-check` resolves it from the same tsconfig. Reordering or flattening the `extends` array does
not change it. It also does not read `compilerOptions.experimental.async` from `svelte.config.js`, so
it rejects `await` in a component that legitimately enables it.

Both are **fail-closed** — noise, not silence — which is the right direction, but a type gate that
reports 23 errors that are not there cannot be the gate. `check` stays on `svelte-check`; re-run this
on each release.

## TypeScript 7

`typescript@7` is the native Go compiler: platform binaries, `tsc.js`, `getExePath.js`, and **no
JavaScript compiler API**. `svelte-check` calls `ts.sys.useCaseSensitiveFileNames` and dies instantly:

```
TypeError: Cannot read properties of undefined (reading 'useCaseSensitiveFileNames')
```

So TypeScript 7 cannot be the `typescript` dependency while `svelte-check` is in the toolchain:

- `typescript@6.0.3` — resolved by `svelte-check`, which owns `.svelte`.
- `@typescript/native-preview` (`tsgo` — the TS 7 engine under a second package name so both install)
  — the `check:tsgo` task, over every package whose sources are pure TypeScript: `@rask/api`,
  `@rask/engine`, `@rask/labeling`, `@rask/explorer-api`, `@rask/zone-contract`, plus media's core.

`tsgo` cannot resolve `*.svelte` imports either, so it could not own the component surface even if
`svelte-check` were gone. **Every line of TypeScript that TypeScript 7 can check, it checks.** The
remaining gap is `.svelte`, and `rsvelte-check --tsgo` is the only thing that closes it.

## The exit condition

Delete this file when `rsvelte-check --tsgo` resolves `$env/*` on every zone and honours
`compilerOptions.experimental.async`. That removes `svelte-check` and `typescript@6`, and `check` /
`check:tsgo` collapse into one task on one compiler.
