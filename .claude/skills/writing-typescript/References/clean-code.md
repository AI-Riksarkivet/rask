# Clean Code (TypeScript)

Robert C. Martin's _Clean Code_ catalog (Chapter 17), adapted to TypeScript. Use this for both authoring new code and reviewing existing code. Rules are numbered so reviews can cite them ("F1 violation: 7 arguments → use a parameter object").

## Quick-reference card

| Code    | Rule                             | One-liner                                               |
| ------- | -------------------------------- | ------------------------------------------------------- |
| **F1**  | Max arguments                    | ≤3 args; more = parameter object                        |
| **F2**  | No output args                   | Return values; don't mutate inputs                      |
| **F3**  | No flag args                     | Boolean flag = two functions hiding as one              |
| **F4**  | Delete dead functions            | Git remembers; no "just in case"                        |
| **N1**  | Descriptive names                | `SECONDS_PER_DAY`, not `d`                              |
| **N4**  | Unambiguous names                | `renameFile(oldPath, newPath)`, not `rename(a, b)`      |
| **N5**  | Length matches scope             | Short for tiny scope, long for globals                  |
| **N6**  | No encodings                     | `users`, not `arrUsers`; `User`, not `IUser`            |
| **N7**  | Names describe side effects      | `getOrCreateConfig`, not `getConfig` (that creates)     |
| **C1**  | No metadata in comments          | Author/ticket/date → Git                                |
| **C3**  | No redundant comments            | `i += 1; // increment i` — delete                       |
| **C5**  | No commented-out code            | Delete it. Git remembers.                               |
| **G5**  | DRY                              | One authoritative representation per piece of knowledge |
| **G9**  | Delete dead code                 | If it's not called, delete it                           |
| **G16** | No obscured intent               | Clear beats clever                                      |
| **G23** | Polymorphism over if/else chains | Discriminated unions + exhaustive switch                |
| **G25** | Named constants                  | No magic numbers                                        |
| **G30** | Functions do one thing           | If you can extract another function, it did two         |
| **G36** | Law of Demeter                   | One dot. No `a.b.c.d.value`                             |
| **T1**  | Test everything that could break | Coverage is a guide, not a goal                         |
| **T5**  | Test boundary conditions         | Bugs cluster at boundaries                              |
| **T9**  | Tests must be fast               | <100ms unit tests                                       |

The rest of this file is the full catalog with examples. Skim the table; read the section you're enforcing.

## Functions (F1-F4)

### F1 — Max 3 arguments

Above 3, your function is doing too much OR you need a data structure.

```typescript
// BAD
function createUser(
	name: string,
	email: string,
	age: number,
	country: string,
	timezone: string,
	language: string,
	newsletter: boolean,
) {
	/* ... */
}

// GOOD
type UserInput = {
	name: string;
	email: string;
	age: number;
	country: string;
	timezone: string;
	language: string;
	newsletter: boolean;
};

function createUser(input: UserInput) {
	/* ... */
}
```

Component props count too. A Svelte component with 7 props is hiding two components.

### F2 — No output arguments

Don't mutate inputs as side effects. Return new values.

```typescript
// BAD
function appendFooter(report: Report): void {
	report.content += '\n---';
}

// GOOD
function withFooter(report: Report): Report {
	return { ...report, content: `${report.content}\n---` };
}
```

Mutating an argument forces callers to remember which functions mutate. Returning new values is honest.

### F3 — No flag arguments

A boolean flag almost always means the function is two functions.

```typescript
// BAD
function render(isTest: boolean) {
	if (isTest) renderTestPage();
	else renderProductionPage();
}

// GOOD
function renderTestPage() {
	/* ... */
}
function renderProductionPage() {
	/* ... */
}
```

If you can't avoid a flag (e.g., a single config knob like `caseSensitive`), split the implementation internally and keep the public surface clean.

### F4 — Delete dead functions

Unused functions are dead weight. Delete them. Git remembers. "Just in case" is the wrong reason to keep code.

## Names (N1-N7)

### N1 — Descriptive names

Names should reveal intent. If a name needs a comment, the name is wrong.

```typescript
// BAD
const d = 86400;
function proc(values: number[]) {
	return values.filter((v) => v > 0);
}

// GOOD
const SECONDS_PER_DAY = 86400;
function filterPositive(numbers: number[]) {
	return numbers.filter((n) => n > 0);
}
```

### N2 — Right abstraction level

Don't leak implementation details into names.

```typescript
// BAD — leaks Map
function getMapOfUserIdsToNames() {
	/* ... */
}

// GOOD — abstract concept
function getUserDirectory() {
	/* ... */
}
```

### N3 — Standard nomenclature

Use domain terms and well-known patterns: `UserFactory`, `OrderRepository`, `calculateAmortization`. Don't invent vocabulary.

### N4 — Unambiguous names

```typescript
// BAD — rename what?
function rename(source: string, target: string) {
	/* ... */
}

// GOOD
function renameFile(oldPath: string, newPath: string) {
	/* ... */
}
```

### N5 — Length matches scope

Short names for tiny scopes; long names for module-level constants.

```typescript
// Tiny scope: 1-letter is fine
const total = numbers.reduce((s, n) => s + n, 0);

// Module-level: descriptive
const MAX_RETRY_ATTEMPTS_BEFORE_DEAD_LETTER = 5;

// BAD — short name, module-level
const MAX = 5;
```

### N6 — No encodings

Don't encode type or scope into names. The compiler knows the type.

```typescript
// BAD — Hungarian notation
const strName = 'Alice';
const arrUsers: string[] = [];
const nCount = 0;

// BAD — `I` prefix for interfaces
interface IUserRepository {
	/* ... */
}

// GOOD
const name = 'Alice';
const users: string[] = [];
const count = 0;
interface UserRepository {
	/* ... */
}
```

### N7 — Names describe side effects

If `getConfig` also creates a config file, rename it.

```typescript
// BAD — hidden side effect
function getConfig(path: string): Config {
	if (!fs.existsSync(path)) fs.writeFileSync(path, '{}'); // creates!
	return JSON.parse(fs.readFileSync(path, 'utf-8'));
}

// GOOD — honest name
function getOrCreateConfig(path: string): Config {
	/* ... */
}
```

## Comments (C1-C5)

Default: **no comments**. The code is the documentation. Comment only when there's a _why_ that's non-obvious (a workaround, a hidden constraint, a surprising invariant). Removing the comment shouldn't confuse a future reader.

### C1 — No metadata

No author names, no ticket numbers, no dates in comments. That's Git's job.

```typescript
// BAD
// Author: Alice  (2024-03-15)
// Ticket: PROJ-1234
// Fixes the issue where users couldn't log in.
function login() {
	/* ... */
}

// GOOD — no comment needed; the function name and Git history do the job
function login() {
	/* ... */
}
```

### C2 — Delete obsolete comments

If the code changed and the comment didn't, the comment lies. Delete it.

### C3 — No redundant comments

```typescript
// BAD
i += 1; // increment i
user.save(); // save the user

// OK (if the *why* is non-obvious)
i += 1; // compensate for 1-based indexing in the rendered list
```

### C4 — Write comments well

Brief. Correct grammar. Don't ramble. Explain _why_, not _what_.

### C5 — Never commit commented-out code

```typescript
// DELETE THIS — an abomination
// function oldCalculate(amount: number) {
//   return amount * 0.15;
// }
```

Git remembers everything. Commented-out code rots, misleads, and gets in the way of search.

## General (G-rules — the big ones)

### G3 — Handle boundary conditions

Empty arrays, page zero, off-by-one, null inputs, max values. Write tests for these (T5).

### G5 — DRY

Every piece of knowledge has one authoritative representation.

```typescript
// BAD — tax rate duplicated three places
const caTotal = subtotal * 1.0825;
const nyTotal = subtotal * 1.07;
const txTotal = subtotal * 1.0625;

// GOOD
const TAX_RATES: Record<string, number> = { CA: 0.0825, NY: 0.07, TX: 0.0625 };
function totalWithTax(subtotal: number, state: string): number {
	return subtotal * (1 + (TAX_RATES[state] ?? 0));
}
```

But: three similar lines is better than a premature abstraction. Wait for the third or fourth occurrence before extracting.

### G9 — Delete dead code

Unreferenced exports, unused parameters, branches that can't be reached. Delete them. Tools (`ts-prune`, `knip`, `eslint --rule no-unused-vars`) help.

### G16 — No obscured intent

Don't be clever. Be clear.

```typescript
// BAD — what does this do?
return ((x & 0x0f) << 4) | (y & 0x0f);

// GOOD
return packCoordinates(x, y);
```

### G23 — Polymorphism over if/else chains

If you find yourself adding cases to a long `if/else if` chain, reach for a discriminated union or a lookup table.

```typescript
// BAD — grows forever
function calculatePay(employee: {
	type: 'SALARIED' | 'HOURLY' | 'COMMISSIONED';
	salary?: number;
	hours?: number;
	rate?: number;
	base?: number;
	commission?: number;
}): number {
	if (employee.type === 'SALARIED') return employee.salary ?? 0;
	if (employee.type === 'HOURLY') return (employee.hours ?? 0) * (employee.rate ?? 0);
	if (employee.type === 'COMMISSIONED') return (employee.base ?? 0) + (employee.commission ?? 0);
	return 0;
}

// GOOD — discriminated union, exhaustive switch
type Employee =
	| { type: 'salaried'; salary: number }
	| { type: 'hourly'; hours: number; rate: number }
	| { type: 'commissioned'; base: number; commission: number };

function calculatePay(e: Employee): number {
	switch (e.type) {
		case 'salaried':
			return e.salary;
		case 'hourly':
			return e.hours * e.rate;
		case 'commissioned':
			return e.base + e.commission;
		default: {
			const _: never = e;
			return _;
		}
	}
}
```

The discriminated-union version: (1) makes invalid states unrepresentable (an hourly employee can't have a `commission` field), (2) gets exhaustiveness checking, (3) is shorter.

### G25 — Named constants over magic numbers

```typescript
// BAD
if (elapsed > 86400) expireSession();
setTimeout(check, 30000);

// GOOD
const SECONDS_PER_DAY = 86400;
const HEALTH_CHECK_INTERVAL_MS = 30_000;
if (elapsed > SECONDS_PER_DAY) expireSession();
setTimeout(check, HEALTH_CHECK_INTERVAL_MS);
```

Numeric separator (`30_000`) helps with large numbers.

### G30 — Functions do one thing

If you can extract another well-named function, your function did two things.

### G36 — Law of Demeter

One dot.

```typescript
// BAD — train wreck
const outputDir = context.options.scratchDir.absolutePath;

// GOOD
const outputDir = context.getScratchDir();
```

Reaching through multiple objects couples you to the entire chain. One change anywhere breaks you.

## Tests (T1-T9)

### T1 — Test everything that could break

Happy path AND edge cases AND error paths. Coverage tools point at gaps; they're a guide, not the goal.

### T5 — Test boundary conditions

```typescript
test('pagination boundaries', () => {
	const items = Array.from({ length: 100 }, (_, i) => i);
	expect(paginate(items, 1, 10)).toEqual(items.slice(0, 10)); // first
	expect(paginate(items, 10, 10)).toEqual(items.slice(90, 100)); // last
	expect(paginate(items, 11, 10)).toEqual([]); // past end
	expect(() => paginate(items, 0, 10)).toThrow(RangeError); // invalid
	expect(paginate([], 1, 10)).toEqual([]); // empty input
});
```

### T6 — Exhaustively test near bugs

When you find a bug, write tests for all similar cases. Bugs cluster.

### T9 — Tests must be fast

Slow tests don't get run. Unit tests under 100 ms; hit real databases / network only in integration tests.

See `testing.md` for the full Vitest reference.

## The Boy Scout rule

Every time you edit a file, leave it a little cleaner. Not perfect — better.

| When you're already touching code, look for | Apply rule                           |
| ------------------------------------------- | ------------------------------------ |
| A cryptic identifier                        | Rename it (N1, N6)                   |
| A redundant comment or commented-out code   | Delete it (C3, C5)                   |
| A magic number                              | Extract to a named constant (G25)    |
| A dead function or unused import            | Delete it (F4, G9)                   |
| A deeply nested block                       | Extract a well-named function (G30)  |
| A boolean flag parameter                    | Split the function (F3)              |
| A long if/else chain                        | Convert to discriminated union (G23) |

Keep changes proportional. Don't refactor unrelated modules to "make the diff better"; that hurts review. Clean up what you're already touching.

## When reviewing code

Cite rules by code. Concrete > vague:

- "F1 violation: 7 args → parameter object."
- "G25: `86400` → `SECONDS_PER_DAY`."
- "T5 gap: no test for empty input."
- "N6: `IUserRepo` → drop the `I` prefix."

Citation makes the discussion about the rule, not about taste.
