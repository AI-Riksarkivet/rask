# Code Style

Toolchain, `tsconfig.json`, file organization, import style. Project-aligned: Bun + Vite + Vitest.

## Toolchain

| Tool | Purpose | Notes |
|---|---|---|
| **bun** | Runtime + package manager + test runner | Replaces `npm`/`pnpm`/`yarn`/`node`/`jest`. |
| **vite** | Frontend dev server + library bundler | Used by SvelteKit and the component library. |
| **vitest** | Test runner | Compatible with Jest API; Vite-native. |
| **eslint** | Linter | Project config in `eslint.config.js` (flat config). |
| **prettier** | Formatter | Project config in `.prettierrc`. |
| **TypeScript** | Type checker | Run via `bun run check` (which usually wraps `svelte-check` + `tsc --noEmit`). |

## `tsconfig.json` essentials

Required compiler options for any project module:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true,
    "esModuleInterop": false
  }
}
```

### Why each one

- **`strict: true`** — enables all strict checks (`strictNullChecks`, `noImplicitAny`, etc.). Non-negotiable.
- **`noUncheckedIndexedAccess: true`** — `array[i]` returns `T | undefined`, forcing you to handle missing indices. Catches a whole class of bugs.
- **`exactOptionalPropertyTypes: true`** — `{ foo?: string }` doesn't silently accept `undefined`. You explicitly opt in with `{ foo?: string | undefined }`.
- **`isolatedModules: true`** — required for Vite/SWC/esbuild. Prevents constructs that can't be compiled file-by-file.
- **`verbatimModuleSyntax: true`** — `import` and `export` keep their original form; no surprise erasure. Pair with inline `type` markers (see imports below).
- **`moduleResolution: "bundler"`** — for Vite/Bun. Use `"node16"` only for Node.js libraries.
- **`esModuleInterop: false`** — when using `verbatimModuleSyntax`, interop is unnecessary. Importing CommonJS modules should use `import * as foo from 'foo'`.

### `extends` does NOT recursively merge

`compilerOptions.paths` in a child tsconfig **replaces** the parent's paths entirely — it doesn't merge. Re-declare every path in the child or hoist them to the root.

```json
// base.json
{ "compilerOptions": { "paths": { "$lib/*": ["./src/lib/*"] } } }

// child.json — $lib is GONE here, only $components exists
{
  "extends": "./base.json",
  "compilerOptions": { "paths": { "$components/*": ["./src/components/*"] } }
}
```

## Imports

### Inline `type` marker (with `verbatimModuleSyntax`)

```typescript
// GOOD — mixed value and type imports
import { type User, getUser } from './user';

// AVOID — separate type-only import (still works, just verbose)
import type { User } from './user';
import { getUser } from './user';

// WRONG with verbatimModuleSyntax — emits a value import for User
import { User, getUser } from './user';
```

### Import order

1. Built-in Node/Bun modules (`node:fs`, `bun:test`)
2. External packages (`bun add` deps)
3. Aliased project imports (`$lib/*`, `$components/*`)
4. Relative imports (`./foo`, `../bar`)
5. Type-only imports (when not using inline `type`)
6. Side-effect imports (`import './styles.css'`) — last

`eslint-plugin-import` or `simple-import-sort` enforces this automatically.

### Don't use barrel exports for everything

```typescript
// AVOID — defeats tree-shaking and slows down IDE go-to-definition
// lib/index.ts
export * from './user';
export * from './product';
export * from './order';

// PREFER — explicit re-exports OR direct imports
// lib/index.ts
export { User } from './user';
export type { UserService } from './user-service';

// Or just import directly
import { User } from '$lib/user';
```

Barrel exports are fine for **library entry points** (`packages/oxen_componets/src/lib/index.ts`) where the public API needs a single import path. Avoid them inside the same package.

## File and identifier naming

| Item | Style | Example |
|---|---|---|
| Files (utility) | `kebab-case.ts` | `api-client.ts`, `parse-config.ts` |
| Files (Svelte components) | `PascalCase.svelte` | `Button.svelte`, `OrderCard.svelte` |
| Functions, variables | `camelCase` | `fetchUser`, `parseConfig` |
| Types, interfaces, classes | `PascalCase` | `User`, `OrderService` |
| Type parameters | `PascalCase`, descriptive | `<TItem>` not `<T>` when scope is large |
| Constants (module-level) | `SCREAMING_SNAKE_CASE` | `MAX_RETRIES`, `API_BASE_URL` |
| Booleans | prefix with `is`/`has`/`can`/`should` | `isLoading`, `hasPermission` |
| Event handlers (props) | `onX` (lowercase, Svelte 5 convention) | `onclick`, `onsubmit` |
| Event handler implementations | `handleX` | `handleSubmit`, `handleClose` |

## Style guidelines

- Use `const` by default. `let` only when reassignment is needed. Never `var`.
- Prefer `interface` for **object shapes that might be extended** (component props, public APIs). Use `type` for **unions, intersections, mapped types, computed types**. When in doubt, `type`.
- Mark fields `readonly` when they shouldn't mutate. Doesn't change runtime behavior; catches bugs at compile time.
- Don't write JSDoc to repeat types — the types ARE the docs. Write JSDoc only when there's a *why* that's non-obvious.
- One responsibility per file. If you have `utils.ts` over 200 lines, it's two files pretending to be one.

## Bun-specific notes

- **Bun APIs (`Bun.file`, `Bun.serve`, `Bun.write`)** only work in the Bun runtime, not in the browser. Keep them out of `components/apps/frontend/src/`.
- **`bun test`** auto-discovers `*.test.ts` and `*.spec.ts`. No config needed for simple cases — but the project uses Vitest, so use `bun test` only for Bun-runtime tests (CLI tools, scripts).
- **`bunx <pkg>`** is the equivalent of `npx`. Use it for one-off tool invocations.
