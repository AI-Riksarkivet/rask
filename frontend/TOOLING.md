# Frontend toolchain

**bun + oxlint + rsvelte-fmt.** One linter and one formatter for every file in the workspace. No workspace
package declares `eslint` or `prettier`, and none of their configs exist.

`prettier` is still in the installed tree, pulled in by the formatter itself: `@rsvelte/fmt` depends on
`prettier-plugin-tailwindcss`, which peer-depends on `prettier`. Nothing here configures or invokes it,
and no `package.json` asks for it; audit that claim with
`grep -rn '"\(eslint\|prettier\)' --include=package.json`.

| Surface                                  | Lint                                  | Format                    | Type-check                                    |
| ---------------------------------------- | ------------------------------------- | ------------------------- | --------------------------------------------- |
| `.ts` `.js` `.mjs` (incl. `*.svelte.ts`) | **oxlint**                            | **rsvelte-fmt** (→ oxfmt) | `tsgo` (TypeScript 7) on `@rask/api`          |
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

`no-navigation-without-base` and `no-goto-without-base` are ON, at the preset's warning level. The
console has no base path, so their warnings name links that are correct; they do not fail `lint`.

## Type-checking

`svelte-check` owns `.svelte` and runs on TypeScript 6. `typescript@7` (the native Go compiler) ships no
JavaScript compiler API, and `svelte-check` calls into it, so TypeScript 7 cannot be the `typescript`
dependency while `svelte-check` is in the toolchain. `@typescript/native-preview` (`tsgo`, the TS 7
engine under a second package name) checks `@rask/api`, which is pure TypeScript.

`@rsvelte/svelte-check` takes `--tsgo` and would put the whole workspace on TypeScript 7. Re-measure it
on each release; it replaces `svelte-check` once it resolves SvelteKit's generated modules and honours
`compilerOptions.experimental.async` from `svelte.config.js`.

## The Arrow patch

`patches/apache-arrow@21.2.0.patch` replaces apache-arrow's compiled null check
(`builder/valid`, a `new Function`) with a closure of the same semantics. The console's content security
policy has no `'unsafe-eval'`, and without the patch every browser-side Arrow encode (the insert-rows and
create-with-rows forms) fails with a CSP violation. Re-check on an apache-arrow upgrade.
