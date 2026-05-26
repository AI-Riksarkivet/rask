# Patterns

Everyday TypeScript patterns used in this project: Result types, Zod validation, custom errors, async, module organization.

## Result types over throwing

Throw for **bugs and programmer errors**. Return `Result<T, E>` for **expected failures** that callers must handle (validation, not-found, conflict, third-party API errors).

```typescript
type Result<T, E = Error> = { ok: true; value: T } | { ok: false; error: E };

function ok<T>(value: T): Result<T, never> {
	return { ok: true, value };
}

function err<E>(error: E): Result<never, E> {
	return { ok: false, error };
}
```

Usage:

```typescript
async function fetchUser(id: string): Promise<Result<User, ApiError>> {
	try {
		const response = await fetch(`/users/${id}`);
		if (response.status === 404) return err(new ApiError('NOT_FOUND', 404));
		if (!response.ok) return err(new ApiError('REQUEST_FAILED', response.status));
		const data = await response.json();
		return ok(UserSchema.parse(data));
	} catch (e) {
		return err(new ApiError('NETWORK', 0, { cause: e }));
	}
}

// Caller
const result = await fetchUser(userId);
if (!result.ok) {
	if (result.error.code === 'NOT_FOUND') return showNotFound();
	return showError(result.error);
}
const user = result.value; // typed as User
```

The compiler forces the caller to discriminate on `result.ok` before touching `value` or `error`. No try/catch needed at the call site — failure is a value.

## Custom errors

Subclass `Error`, set `name`, optionally add `cause`.

```typescript
export class AppError extends Error {
	constructor(
		message: string,
		public readonly code: string,
		options?: { cause?: unknown },
	) {
		super(message, options);
		this.name = 'AppError';
	}
}

export class NotFoundError extends AppError {
	constructor(resource: string, id: string) {
		super(`${resource} not found: ${id}`, 'NOT_FOUND');
		this.name = 'NotFoundError';
	}
}

export class ApiError extends AppError {
	constructor(
		code: string,
		public readonly statusCode: number,
		options?: { cause?: unknown },
	) {
		super(`API ${code} (${statusCode})`, code, options);
		this.name = 'ApiError';
	}
}
```

Always:

- Set `name` in the constructor (matches the class name; used by error formatters).
- Forward `cause` via `super(message, { cause })`. Preserves the underlying error for debugging.
- Prefer narrow error types over one giant `AppError` with a magic `code` field. The compiler can't help you with magic strings.

## Validation with Zod

Project standard for any external input — HTTP responses, env vars, message payloads, `localStorage`.

```typescript
import { z } from 'zod';

const UserSchema = z.object({
	id: z.string().uuid(),
	email: z.string().email(),
	name: z.string().min(1),
	role: z.enum(['admin', 'user', 'guest']),
});

type User = z.infer<typeof UserSchema>; // type derived from schema — single source of truth

function parseUser(data: unknown): User {
	return UserSchema.parse(data); // throws on invalid
}

function safeParseUser(data: unknown): Result<User, z.ZodError> {
	const result = UserSchema.safeParse(data);
	if (result.success) return ok(result.data);
	return err(result.error);
}
```

**`z.infer<typeof Schema>`** keeps the schema as the single source of truth. Don't write a separate `type User` declaration that can drift.

For environment variables:

```typescript
const EnvSchema = z.object({
	PUBLIC_API_URL: z.string().url(),
	PUBLIC_FEATURE_FLAG: z.enum(['on', 'off']).default('off'),
});

export const env = EnvSchema.parse(import.meta.env); // Vite / SvelteKit
```

Validation happens **once at boundary entry**. After that, the data is typed and trustworthy.

## Async patterns

### Concurrent requests

```typescript
// All or nothing — first failure rejects the whole call
async function fetchAll<T>(urls: string[]): Promise<T[]> {
	const responses = await Promise.all(urls.map((u) => fetch(u)));
	return Promise.all(responses.map((r) => r.json() as Promise<T>));
}

// All complete, even if some fail — get individual outcomes
async function fetchAllSettled<T>(urls: string[]): Promise<Result<T>[]> {
	const settled = await Promise.allSettled(
		urls.map((u) => fetch(u).then((r) => r.json() as Promise<T>)),
	);
	return settled.map((s) => (s.status === 'fulfilled' ? ok(s.value) : err(s.reason)));
}
```

Use `Promise.all` when failure of any one means the whole operation fails. Use `Promise.allSettled` when partial success is meaningful (e.g., fetching multiple independent resources for a dashboard).

### Retry with exponential backoff

```typescript
async function retry<T>(
	fn: () => Promise<T>,
	options: { attempts: number; baseDelayMs: number; maxDelayMs?: number } = {
		attempts: 3,
		baseDelayMs: 1000,
	},
): Promise<T> {
	const { attempts, baseDelayMs, maxDelayMs = 30_000 } = options;
	let lastError: unknown;
	for (let i = 0; i < attempts; i++) {
		try {
			return await fn();
		} catch (e) {
			lastError = e;
			if (i === attempts - 1) break;
			const delay = Math.min(baseDelayMs * 2 ** i, maxDelayMs);
			const jitter = Math.random() * delay * 0.5;
			await new Promise((r) => setTimeout(r, delay + jitter));
		}
	}
	throw lastError;
}
```

Only retry **idempotent** operations. A failed POST can mean two charges if you retry without an idempotency key. For one-off scripts and CLIs, retry libraries (`p-retry`, `async-retry`) are fine; for app code, this short helper covers most cases.

### Abort signals

```typescript
async function fetchWithTimeout(url: string, timeoutMs: number): Promise<Response> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		return await fetch(url, { signal: controller.signal });
	} finally {
		clearTimeout(timer);
	}
}
```

Modern alternative: `AbortSignal.timeout(timeoutMs)`. Either works.

## Module organization

### Public API of a library package

For the `packages/oxen_componets/` library, expose only what consumers should import.

```typescript
// src/lib/index.ts — public API barrel
export { Button } from './components/button';
export { Dialog } from './components/dialog';
export type { ButtonProps, DialogProps } from './types';
// Internal helpers (cn, utils) are NOT re-exported
```

Consumers import from `oxen_componets`, not deep paths:

```typescript
// GOOD
import { Button } from 'oxen_componets';

// AVOID (couples consumers to internal layout)
import { Button } from 'oxen_componets/src/lib/components/button/button.svelte';
```

### Internal modules

Inside the same package, prefer direct paths over barrels. Barrels slow down IDE go-to-definition and defeat tree-shaking inside the package.

```typescript
// In components/apps/frontend/src/lib/api.ts
import { parseConfig } from './parse-config'; // direct
import { logger } from '$lib/logger'; // aliased — also direct
```

## Dependency injection (when needed)

Most TS code in this project doesn't need DI containers — closures and plain functions are simpler.

```typescript
type Dependencies = {
	fetchUser: (id: string) => Promise<User>;
	logger: Logger;
};

export function createUserService({ fetchUser, logger }: Dependencies) {
	return {
		async getProfile(id: string) {
			logger.info('Fetching user', { id });
			return fetchUser(id);
		},
	};
}

// Wire at startup
const service = createUserService({
	fetchUser: realFetchUser,
	logger: realLogger,
});

// Test with fakes
const testService = createUserService({
	fetchUser: async () => fakeUser,
	logger: silentLogger,
});
```

Closure-based "constructor functions" return objects implementing a typed interface. No `class`, no `@inject`, no framework — and trivially testable.

## Configuration

```typescript
import { z } from 'zod';

const ConfigSchema = z.object({
	PORT: z.coerce.number().int().positive().default(3000),
	PUBLIC_API_URL: z.string().url(),
	NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
});

export const config = ConfigSchema.parse(import.meta.env);
```

- Schema lives next to where it's used (or in a shared `config.ts`).
- Parse **once at startup** — fail loudly with a useful error message before anything tries to run.
- `z.coerce.number()` handles env vars (which are strings).
- Defaults belong in the schema, not in code that reads `config.PORT ?? 3000`.

## Style guidelines

- `const` by default; `let` only when reassignment is necessary; never `var`.
- Prefer `interface` for object shapes that may be extended (component props), `type` for unions/intersections/mapped types.
- Mark fields `readonly` when they shouldn't mutate.
- Functions: arrow expressions for callbacks (`map`, `filter`), `function` declarations for top-level named functions.
- No abbreviations: `request`, not `req`; `user`, not `usr` (except well-known like `id`, `url`, `db`).
- Use `??` over `||` for default values when `''` / `0` / `false` are valid inputs.
- Optional chaining (`?.`) for safe access on possibly-null values, not for "I don't want to think about null".
