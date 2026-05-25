# Testing (Vitest)

Vitest-based test patterns for both the SvelteKit app and the component library. For Clean Code test rules (T1-T9, F.I.R.S.T.), see `clean-code.md` § Tests.

## Setup

Project uses Vitest. Install if a fresh package:

```bash
bun add -d vitest @vitest/coverage-v8
# For DOM/component tests:
bun add -d jsdom @testing-library/svelte @testing-library/jest-dom
```

## Configuration

`vitest.config.ts`:

```typescript
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,                       // describe/it/expect without imports
    environment: 'jsdom',                // 'node' for non-DOM tests
    setupFiles: ['./tests/setup.ts'],
    coverage: {
      reporter: ['text', 'html'],
      exclude: ['node_modules/', 'tests/', '**/*.d.ts', '**/*.config.ts'],
      thresholds: { lines: 80, functions: 80, statements: 80, branches: 70 },
    },
  },
});
```

For a SvelteKit project, prefer `defineConfig` from `vite` and merge plugins — the `vitest/config` form above works for non-SvelteKit packages (libraries, utilities).

`tests/setup.ts`:

```typescript
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/svelte';

afterEach(() => {
  cleanup();
});
```

## Unit tests

```typescript
import { describe, it, expect } from 'vitest';
import { validateEmail } from './validate-email';

describe('validateEmail', () => {
  it('accepts a valid email', () => {
    expect(validateEmail('user@example.com')).toBe(true);
  });

  it('rejects an empty string', () => {
    expect(validateEmail('')).toBe(false);
  });

  it('rejects a missing @', () => {
    expect(validateEmail('userexample.com')).toBe(false);
  });

  it('rejects a missing domain', () => {
    expect(validateEmail('user@')).toBe(false);
  });
});
```

One concept per test (T9, F.I.R.S.T.). Descriptive test names — they double as documentation.

## Async tests

```typescript
import { describe, it, expect } from 'vitest';
import { fetchUser } from './api';

describe('fetchUser', () => {
  it('returns user data on success', async () => {
    const user = await fetchUser('123');
    expect(user.id).toBe('123');
  });

  it('throws NotFoundError when user does not exist', async () => {
    await expect(fetchUser('missing')).rejects.toThrow('User not found');
  });

  it('throws NotFoundError of correct type', async () => {
    await expect(fetchUser('missing')).rejects.toBeInstanceOf(NotFoundError);
  });
});
```

`.rejects.toThrow()` for async functions that throw. Don't wrap in try/catch — that hides failures.

## Boundary tests (T5)

Bugs cluster at boundaries. Test them explicitly.

```typescript
import { describe, it, expect } from 'vitest';
import { paginate } from './paginate';

describe('paginate boundaries', () => {
  const items = Array.from({ length: 100 }, (_, i) => i);

  it('first page returns first slice', () => {
    expect(paginate(items, 1, 10)).toEqual(items.slice(0, 10));
  });

  it('last page returns last slice', () => {
    expect(paginate(items, 10, 10)).toEqual(items.slice(90, 100));
  });

  it('past last page returns empty', () => {
    expect(paginate(items, 11, 10)).toEqual([]);
  });

  it('page zero throws RangeError', () => {
    expect(() => paginate(items, 0, 10)).toThrow(RangeError);
  });

  it('empty input returns empty', () => {
    expect(paginate([], 1, 10)).toEqual([]);
  });

  it('page size larger than input returns all', () => {
    expect(paginate(items.slice(0, 5), 1, 10)).toEqual(items.slice(0, 5));
  });
});
```

## Mocking

### Function mocks

```typescript
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

describe('getUser', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls fetch with the right URL', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ id: '123', email: 'a@b.com' }),
    });

    await getUser('123');

    expect(mockFetch).toHaveBeenCalledWith('/api/users/123');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});
```

### Module mocks

```typescript
import { vi, describe, it, expect } from 'vitest';
import { sendEmail } from './email';
import { createUser } from './user-service';

vi.mock('./email', () => ({
  sendEmail: vi.fn(),
}));

describe('createUser', () => {
  it('sends a welcome email', async () => {
    await createUser({ email: 'test@example.com', name: 'Test' });

    expect(sendEmail).toHaveBeenCalledWith(
      expect.objectContaining({ to: 'test@example.com' }),
    );
  });
});
```

### Partial mocks

```typescript
vi.mock('./email', async () => {
  const actual = await vi.importActual<typeof import('./email')>('./email');
  return {
    ...actual,
    sendEmail: vi.fn(),       // only mock this one
  };
});
```

### Spying on existing functions

```typescript
import * as api from './api';

const spy = vi.spyOn(api, 'fetchUser').mockResolvedValue(fakeUser);
// ... test
spy.mockRestore();  // important in afterEach
```

## Svelte 5 component tests

For Svelte 5 components (project uses `oxen_componets` with `@testing-library/svelte`):

```typescript
import { render, screen } from '@testing-library/svelte';
import { describe, it, expect } from 'vitest';
import userEvent from '@testing-library/user-event';
import Button from './Button.svelte';

describe('Button', () => {
  it('renders its label', () => {
    render(Button, { props: { label: 'Click me', onclick: () => {} } });
    expect(screen.getByText('Click me')).toBeInTheDocument();
  });

  it('calls onclick when clicked', async () => {
    const user = userEvent.setup();
    const handleClick = vi.fn();
    render(Button, { props: { label: 'Click', onclick: handleClick } });

    await user.click(screen.getByRole('button'));

    expect(handleClick).toHaveBeenCalledTimes(1);
  });

  it('is disabled when disabled prop is true', () => {
    render(Button, { props: { label: 'Click', onclick: () => {}, disabled: true } });
    expect(screen.getByRole('button')).toBeDisabled();
  });
});
```

For testing patterns specific to Svelte 5 runes (reactive state, derived values, effects), see `svelte-skills:svelte-runes`.

## Test against API responses

Use Vitest's `fetch` mocking or [MSW](https://mswjs.io/) for realistic HTTP mocking:

```typescript
import { setupServer } from 'msw/node';
import { http, HttpResponse } from 'msw';
import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest';

const server = setupServer(
  http.get('/api/users/:id', ({ params }) => {
    if (params.id === '404') return new HttpResponse(null, { status: 404 });
    return HttpResponse.json({ id: params.id, email: 'test@example.com' });
  }),
);

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('getUser', () => {
  it('returns user data on 200', async () => {
    const user = await getUser('123');
    expect(user.id).toBe('123');
  });

  it('throws on 404', async () => {
    await expect(getUser('404')).rejects.toThrow();
  });
});
```

MSW intercepts at the network layer — your code under test makes real `fetch` calls, MSW responds. Closer to integration testing than module mocks.

## Snapshot tests — use sparingly

```typescript
expect(component).toMatchSnapshot();
```

Snapshots are useful for stable, deterministic output (rendered HTML, serialized error messages). They're a maintenance liability for things that change often (component layouts) — every minor change becomes a diff to review.

**Rule:** snapshots for serialized DATA, not for rendered UI.

## Coverage

```bash
bun run vitest --coverage
bun run vitest --coverage --coverage.thresholds.lines=85
```

Aim for **meaningful** coverage. 100% line coverage with shallow tests is worse than 70% with thoughtful boundary tests. Coverage gaps are signals (T2, T8) — investigate them, don't paper them over.

## Vitest CLI tips

```bash
bun run vitest                       # watch mode (default)
bun run vitest run                   # single run, for CI
bun run vitest --ui                  # browser UI
bun run vitest <filename>            # run one file
bun run vitest -t "regex"            # run tests matching name
bun run vitest --reporter=verbose    # show every test
bun run vitest --bail=1              # stop on first failure
```

## Anti-patterns

| ❌ Don't | ✅ Do |
|---|---|
| `test.skip` without a reason | Either fix it or delete it. If keeping, comment WHY. |
| `test.only` committed | Pre-commit hook should reject it. |
| Multiple assertions about different things in one test | One concept per test |
| Tests that hit real databases / network in unit suites | Mock the boundary; use MSW for HTTP |
| Tests that pass when run alone but fail in suite | Tests must be independent (F.I.R.S.T.) |
| Tests with arbitrary `await sleep(500)` | Wait for a specific condition with `waitFor` |
| Asserting `toHaveBeenCalled()` without `toHaveBeenCalledWith()` | Assert the arguments too — that's the contract |
| Reaching into private internals to test | Test public behavior. If you must reach in, refactor first |

## When tests fail

1. **Read the assertion message before the stack trace.** Vitest's assertion errors usually tell you what was expected vs received.
2. **Look at patterns (T7).** If many async tests fail intermittently, the problem isn't the tests — it's the async handling.
3. **Coverage gaps in failing files (T8).** Often reveal which code path actually broke.
4. **Test the bug, then fix it.** Write a failing test that reproduces the bug first; then make it pass. This prevents regressions.
