---
name: writing-typescript
description: Idiomatic TypeScript for this project — strict typing, `unknown` over `any`, Result types over throwing, composition, Bun/Vite/Vitest toolchain. Use when writing or reviewing TypeScript code in the SvelteKit frontend (`components/apps/frontend/`) or the component library (`packages/oxen_componets/`). Defers Svelte-specific concerns (runes, routing, components) to the `svelte-skills:*` plugin.
---

# Writing TypeScript

Language-level TypeScript for this project: strict typing, parse-don't-validate at boundaries, Result types, Bun + Vite + Vitest. Frontend is **Svelte 5 + SvelteKit 2**, not React — defer to `svelte-skills:*` for everything Svelte-specific.

## Scope routing

| If you need to…                                                                                     | Read                        |
| --------------------------------------------------------------------------------------------------- | --------------------------- |
| Configure `tsconfig.json`, set up Bun/Vite, organize imports, file naming                           | `References/code-style.md`  |
| Use `unknown`/generics/discriminated unions/type guards, narrow types, `satisfies` vs `as`          | `References/type-safety.md` |
| Look up everyday patterns — Result types, Zod validation, async, custom errors, module organization | `References/patterns.md`    |
| Audit code against Clean Code rules (functions, names, comments, general, tests)                    | `References/clean-code.md`  |
| Write tests with Vitest (unit, async, mocking, coverage, boundary cases)                            | `References/testing.md`     |

## House style — non-negotiable

- **Strict mode everywhere.** `strict: true`, `noUncheckedIndexedAccess: true`, `exactOptionalPropertyTypes: true`. No exceptions.
- **`unknown`, never `any`.** `any` opts out of the type system silently. `unknown` forces narrowing before use.
- **Parse, don't validate, at boundaries.** Untrusted data (HTTP response, env, message payload) gets a Zod parse — afterwards it's a typed value.
- **Composition over inheritance.** Small focused functions, types/interfaces for shape, no class hierarchies for business logic.
- **`satisfies` for validation, `as` only as a last resort.** `as` widens silently; `satisfies` preserves literal types AND verifies shape.
- **`bun` for everything Node-level.** Not `npm`, not `pnpm`, not `yarn` — `bun install`, `bun run`, `bun test`.
- **No `enum`s.** Use literal union types (`type Role = 'admin' | 'user'`). Enums emit runtime code and don't narrow as cleanly.
- **Result types over throw for expected failures.** Throw for _bugs_; return `Result<T, E>` for _expected_ failures (validation, not found, conflict).

## Quick patterns

```typescript
// Types — modern syntax, no `any`, prefer literal unions
type Role = 'admin' | 'user' | 'guest';
type User = { id: string; email: string; role: Role };

// Type guard — narrows unknown to a typed shape
function isUser(value: unknown): value is User {
	return typeof value === 'object' && value !== null && 'id' in value;
}

// Discriminated union — exhaustive switch with `never` check
type Result<T, E = Error> = { ok: true; value: T } | { ok: false; error: E };

function unwrap<T>(r: Result<T>): T {
	if (r.ok) return r.value;
	throw r.error;
}

// satisfies — verifies shape without widening
const ROLES = {
	admin: { canEdit: true, canDelete: true },
	user: { canEdit: true, canDelete: false },
	guest: { canEdit: false, canDelete: false },
} satisfies Record<Role, { canEdit: boolean; canDelete: boolean }>;
// ROLES.admin.canEdit is still typed as `true`, not `boolean`
```

## Toolchain

```bash
bun install              # install deps from bun.lock
bun add <pkg>            # add a runtime dep
bun add -d <pkg>         # add a dev dep
bun run build            # build (vite)
bun run dev              # dev server (vite)
bun test                 # vitest
bun run lint             # eslint
bun run format           # prettier
```

## Project layout

| Location                                 | Purpose                           | Notes                                                                               |
| ---------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------- |
| `components/apps/frontend/`              | SvelteKit 2 application           | `+page.svelte` routes, `+page.ts` loaders. See `svelte-skills:sveltekit-structure`. |
| `packages/oxen_componets/`               | Shared Svelte 5 component library | Published via Bun workspaces. See `svelte-skills:svelte-components`.                |
| `packages/oxen_componets/vite.config.ts` | Library build config              | Bun + Vite.                                                                         |

## Cross-skill boundaries

- **`svelte-skills:svelte-runes`** — `$state`, `$derived`, `$effect`, `$props`. Anything reactivity-related goes there.
- **`svelte-skills:sveltekit-structure`** — routes, layouts, error boundaries, SSR.
- **`svelte-skills:sveltekit-data-flow`** — load functions, form actions, `+page.server.ts` vs `+page.ts`.
- **`svelte-skills:sveltekit-remote-functions`** — `.remote.ts` files with `command()` / `query()` / `form()`.
- **`svelte-skills:svelte-components`** — component composition, Bits UI / Ark UI / Melt UI patterns.
- **`svelte-skills:svelte-styling`** — scoped styles, `:global`, CSS custom properties.
- **`svelte-skills:svelte-template-directives`** — `{@attach}`, `{@render}`, `{@const}`.
- **`svelte-skills:layerchart-svelte5`** — LayerChart snippets and tooltips.
- This skill is for **TypeScript itself** — types, language idioms, project tooling. If your question is about Svelte syntax, runes, or SvelteKit, read the sibling skill first.

## Boy Scout rule

Every TypeScript change leaves the touched code a little cleaner. Not perfect — better.

- Rename a cryptic variable → `clean-code.md` § Names
- Delete commented-out code → `clean-code.md` § Comments (C5)
- Replace a magic number with a named constant → `clean-code.md` § General (G25)
- Extract a function that's doing two things → `clean-code.md` § Functions (F3, G30)

Keep changes proportional to the task. Don't refactor unrelated modules; do clean up what you're already editing.

## Top gotchas

- **`as` silently widens; `satisfies` preserves literals.** `const x = { foo: 1 } as { foo: number }` loses `1` (becomes `number`). Use `satisfies` when you want both validation AND narrow types.
- **`Array<T>.includes(x)` requires `x: T`.** Narrowing-from-union doesn't work. Wrap in a type-predicate helper: `function isRole(v: string): v is Role { return ROLES.includes(v as Role); }`.
- **`tsconfig.json` `extends` doesn't merge `compilerOptions.paths`.** Child paths _replace_ parent paths entirely. Re-declare everything in the child.
- **`import type` with `verbatimModuleSyntax` + named exports** breaks if you mix value and type imports. Use inline: `import { type Foo, bar } from "mod"`.
- **`strictNullChecks: false` is contagious.** Without it, `T` silently means `T | null | undefined` for _every_ type. Partial migrations leave types that lie about nullability — keep it `true` always.
- **`as const` ≠ `as Foo` ≠ `satisfies Foo`.** `as const` freezes + narrows to literals. `as Foo` asserts (lying allowed). `satisfies Foo` validates without widening. Use `as const` for lookup tables, `satisfies` for typed configs, never `as` without a comment explaining why.
- **Mutable default parameters share state in arrow functions too.** `const f = (xs: number[] = []) => xs.push(1)` reuses the same array. Use `xs?: number[]` and create inside.
- **Bun-only APIs leak into shared code.** `Bun.file()`, `Bun.serve()` aren't available in browser bundles. Frontend code must stay Bun-API-free.
