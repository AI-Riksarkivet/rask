# Type Safety

How to use the type system to prevent bugs at compile time.

## `unknown`, never `any`

`any` opts out of the type system silently. Every `any` is a potential runtime crash with no compile-time warning.

```typescript
// BAD — caller can pass anything, callee can do anything
function parse(data: any): User {
	return data;
}

// GOOD — caller must check before use; callee must narrow before access
function parse(data: unknown): User {
	if (!isUser(data)) throw new Error('Invalid user data');
	return data;
}
```

When you receive untrusted data (HTTP response, JSON.parse, `localStorage.getItem`), type it as `unknown` and narrow once — usually with a Zod schema. After narrowing, the rest of the code is fully typed.

## Type guards

A function that returns `value is T` narrows the type for the caller.

```typescript
function isString(value: unknown): value is string {
	return typeof value === 'string';
}

function isUser(value: unknown): value is User {
	return (
		typeof value === 'object' &&
		value !== null &&
		'id' in value &&
		typeof (value as { id: unknown }).id === 'string'
	);
}

// Usage
function processInput(input: unknown) {
	if (isUser(input)) {
		// input is now typed as User
		console.log(input.email);
	}
}
```

Prefer **Zod parse** over hand-rolled type guards for anything coming from outside (HTTP, files, env). Hand-rolled guards are fine for internal narrowing (union members, optional fields).

## Discriminated unions

The single most useful type pattern in TypeScript. Tagged variants with exhaustive switches.

```typescript
type RequestState<T> =
	| { status: 'idle' }
	| { status: 'loading' }
	| { status: 'success'; data: T }
	| { status: 'error'; error: Error };

function render<T>(state: RequestState<T>): string {
	switch (state.status) {
		case 'idle':
			return 'Idle';
		case 'loading':
			return 'Loading…';
		case 'success':
			// state.data is in scope and typed as T
			return `Got ${state.data}`;
		case 'error':
			// state.error is in scope and typed as Error
			return `Error: ${state.error.message}`;
		default: {
			// Exhaustiveness check — if a new variant is added,
			// this assignment fails to compile.
			const _exhaustive: never = state;
			return _exhaustive;
		}
	}
}
```

The `never` assignment in `default` catches non-exhaustive switches at compile time. Use it whenever exhaustiveness matters.

## Generic constraints

```typescript
// Pluck a typed value from an object
function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] {
	return obj[key];
}

// Require an `id` field
type WithId = { id: string };

function updateById<T extends WithId>(items: readonly T[], id: string, update: Partial<T>): T[] {
	return items.map((item) => (item.id === id ? { ...item, ...update } : item));
}
```

Name type parameters descriptively when scope is large:

```typescript
// In a 3-line generic, T is fine
function id<T>(x: T): T {
	return x;
}

// In a 50-line generic, give it a name
function paginate<TItem>(items: readonly TItem[], page: number, size: number): TItem[] {
	return items.slice((page - 1) * size, page * size);
}
```

## Utility types

| Utility          | What it does                 | Use for                  |
| ---------------- | ---------------------------- | ------------------------ |
| `Partial<T>`     | All fields optional          | Update payloads          |
| `Required<T>`    | All fields required          | After validation         |
| `Readonly<T>`    | All fields readonly          | Immutable shapes         |
| `Pick<T, K>`     | Subset of fields             | API response projections |
| `Omit<T, K>`     | All fields except K          | Strip sensitive fields   |
| `Record<K, V>`   | Object with K keys, V values | Lookup tables            |
| `Exclude<T, U>`  | Remove members of union      | Narrow union types       |
| `Extract<T, U>`  | Keep members of union        | Filter union types       |
| `NonNullable<T>` | Remove null/undefined        | After null check         |
| `ReturnType<F>`  | Return type of function      | Avoid type duplication   |
| `Awaited<T>`     | Unwrap Promise               | Async return types       |
| `Parameters<F>`  | Argument tuple               | Forward function types   |

```typescript
// Real-world example
type UserUpdate = Partial<Omit<User, 'id' | 'createdAt'>>;
//   ^ User fields that can be updated (no id, no timestamps)

type CreateUserInput = Omit<User, 'id' | 'createdAt' | 'updatedAt'>;
//   ^ What the API accepts to create a user
```

## `satisfies` vs `as` vs `as const`

These three are **not interchangeable**. Picking the wrong one silently corrupts your types.

```typescript
type RoleConfig = { canEdit: boolean; canDelete: boolean };

// `as` — asserts a type, doesn't check, widens
const config1 = { canEdit: true, canDelete: false } as RoleConfig;
// config1.canEdit is `boolean` (lost the `true` literal)
// And typos go through silently:
// const broken = { typo: true } as RoleConfig;  // ← compiles!

// `satisfies` — verifies shape, keeps literal types
const config2 = { canEdit: true, canDelete: false } satisfies RoleConfig;
// config2.canEdit is `true` (literal preserved)
// Typos are caught:
// const broken = { typo: true } satisfies RoleConfig;  // ← compile error

// `as const` — freezes everything to literals and readonly
const config3 = { canEdit: true, canDelete: false } as const;
// type is `{ readonly canEdit: true; readonly canDelete: false }`
// No shape validation against RoleConfig

// Combined — validates shape AND preserves literals
const config4 = { canEdit: true, canDelete: false } as const satisfies RoleConfig;
```

| Tool                     | Validates shape     | Preserves literals | Makes readonly |
| ------------------------ | ------------------- | ------------------ | -------------- |
| `as Foo`                 | NO (assertion only) | NO                 | NO             |
| `satisfies Foo`          | YES                 | YES                | NO             |
| `as const`               | NO                  | YES                | YES            |
| `as const satisfies Foo` | YES                 | YES                | YES            |

**Rule:** Use `satisfies` when defining typed configs. Use `as const` for lookup tables / enum-like objects. Use `as` only when you genuinely know better than the compiler AND have written a comment saying why.

## `noUncheckedIndexedAccess`

With this on, `array[i]` returns `T | undefined`. You're forced to handle missing entries.

```typescript
const items = ['a', 'b', 'c'];

// Without noUncheckedIndexedAccess: items[10] is `string` (lies)
// With noUncheckedIndexedAccess: items[10] is `string | undefined` (truth)

const first = items[0];
if (first !== undefined) {
	console.log(first.toUpperCase());
}

// Or use .at() which has the same type
const last = items.at(-1); // string | undefined
```

## Type assertions that aren't lies

If you need an assertion that the compiler can't infer, use a narrow form and write a comment explaining why.

```typescript
// BAD — broad assertion, no explanation
const user = response as User;

// BETTER — narrow assertion via guard
function assertIsUser(value: unknown): asserts value is User {
	if (!isUser(value)) throw new Error('Expected User');
}
assertIsUser(response); // response is now typed as User

// BEST — parse, don't assert
const user = UserSchema.parse(response); // Zod
```

## Literal union types, not enums

```typescript
// BAD — enum (emits runtime code, doesn't narrow as cleanly)
enum Role {
	Admin = 'admin',
	User = 'user',
	Guest = 'guest',
}

// GOOD — literal union (zero runtime cost, fully type-safe)
type Role = 'admin' | 'user' | 'guest';
const ROLES = ['admin', 'user', 'guest'] as const satisfies readonly Role[];

// Type guard for runtime checks
function isRole(value: string): value is Role {
	return (ROLES as readonly string[]).includes(value);
}
```

Enums have legitimate uses (numeric flag enums for interop with C-style APIs) but they're rare. Default to literal unions.

## `Array.includes` doesn't narrow

```typescript
// This doesn't work as expected
function isRole(value: string): boolean {
	return ['admin', 'user', 'guest'].includes(value);
	//                                          ^ Type error: value not in union
}

// Workaround 1: widen the array
function isRole(value: string): value is Role {
	return (ROLES as readonly string[]).includes(value);
}

// Workaround 2: typed Set
const ROLE_SET = new Set<Role>(['admin', 'user', 'guest']);
function isRole(value: string): value is Role {
	return ROLE_SET.has(value as Role);
}
```

## `keyof typeof` for lookup tables

```typescript
const STATUS_MESSAGES = {
	idle: 'Ready',
	loading: 'Working…',
	success: 'Done',
	error: 'Failed',
} as const;

type Status = keyof typeof STATUS_MESSAGES;
// 'idle' | 'loading' | 'success' | 'error'

function getMessage(status: Status): string {
	return STATUS_MESSAGES[status];
}
```

Source of truth is the object, not a separate type declaration. Add a key, and the type updates.

## Branded types for primitive distinctions

```typescript
type UserId = string & { readonly __brand: 'UserId' };
type OrderId = string & { readonly __brand: 'OrderId' };

function makeUserId(s: string): UserId {
	return s as UserId;
}

function getUser(id: UserId): User {
	/* ... */
}

const orderId = 'order_123' as OrderId;
getUser(orderId); // Compile error — UserId expected, OrderId given
```

Branded types prevent mixing up IDs of different entities. Costs zero at runtime.

## `Awaited` for async return types

```typescript
async function fetchUser(id: string): Promise<User> {
	/* ... */
}

// Without Awaited — type is Promise<User>
type R1 = ReturnType<typeof fetchUser>;

// With Awaited — type is User
type R2 = Awaited<ReturnType<typeof fetchUser>>;
```

Useful when wiring functions whose async-ness shouldn't leak into the consumer's type.
