---
name: hcp-frontend
description: >
  HCP frontend design patterns, component conventions, and architecture guide.
  Use when: writing Svelte components, adding features, refactoring, creating
  pages, working with remote functions, settings forms, dialogs, or any
  frontend code in this project.
---

# HCP Frontend — Patterns & Conventions

## Stack

- **SvelteKit 2** + **Svelte 5** (runes mode only)
- **shadcn-svelte** component library (Bits UI primitives)
- **Deno** as runtime + package manager
- **Tailwind CSS** for styling
- Quality: `make quality` runs deno fmt/lint + prettier + svelte-check

## Project Layout

```
frontend/src/
├── lib/
│   ├── components/ui/       # Reusable components (shadcn + custom)
│   ├── utils/               # Composables and helpers
│   ├── constants.ts         # Enums, role lists, shared constants
│   ├── *.remote.ts          # Remote functions (query/command)
│   └── tenant-info.remote.ts, users.remote.ts, etc.
├── routes/
│   ├── (app)/               # Authenticated app routes
│   │   ├── namespaces/      # Namespace list + detail pages
│   │   ├── buckets/         # S3 bucket pages
│   │   ├── users/           # User + group pages
│   │   ├── settings/        # Tenant settings
│   │   └── ...
│   └── login/
```

## Remote Functions

All API calls use SvelteKit remote functions in `*.remote.ts` files.

```typescript
// Read data (auto-cached, reactive)
export const get_thing = query(schema, async (params) => { ... });

// Mutate data
export const update_thing = command(schema, async (params) => { ... });
```

**In components:**
```svelte
let data = $derived(get_thing({ tenant, name }));
let current = $derived(data?.current as Thing);
```

**Mutations refresh queries:**
```typescript
await update_thing({ tenant, body }).updates(data);
```

## Settings Form Pattern

All settings/config forms follow this exact pattern using `useSave`:

```svelte
<script lang="ts">
  import SaveButton from '$lib/components/ui/save-button.svelte';
  import { useSave } from '$lib/utils/use-save.svelte.js';

  // 1. Query the data
  let data = $derived(get_something({ tenant }));
  let current = $derived((data?.current ?? {}) as SomeType);

  // 2. Create saver
  const saver = useSave({
    successMsg: 'Settings updated',
    errorMsg: 'Failed to update settings',
  });

  // 3. Local editable state
  let localField = $state('');

  // 4. Sync from server (re-runs on data change OR after save)
  $effect(() => {
    const c = current;
    void saver.syncVersion;     // <- triggers re-sync after save
    localField = c.field ?? '';
  });

  // 5. Dirty check
  let dirty = $derived(localField !== (current.field ?? ''));
</script>

<!-- 6. SaveButton with inline save logic -->
<SaveButton
  {dirty}
  saving={saver.saving}
  onclick={() =>
    saver.run(async () => {
      if (!data) return;
      await update_something({ tenant, body: { field: localField } })
        .updates(data);
    })}
/>
```

**Key rules:**
- `useSave` handles saving state, syncVersion increment, and toast messages
- The `$effect` MUST read `void saver.syncVersion` to re-sync after save
- The save logic goes inline in the `onclick` callback, NOT in a separate function
- Use `$state` for editable fields, `$derived` for dirty checks and query results

## Dialog Pattern

All create/edit dialogs use `FormDialog`:

```svelte
<script lang="ts">
  import FormDialog from '$lib/components/ui/form-dialog.svelte';
  import { toast } from 'svelte-sonner';

  let { open = $bindable(false) }: { open: boolean } = $props();
  let error = $state('');
  let loading = $state(false);

  async function handleSubmit(e: SubmitEvent) {
    e.preventDefault();
    loading = true;
    error = '';
    try {
      await create_thing({ ... }).updates(queryData);
      toast.success('Created successfully');
      open = false;
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed';
    } finally {
      loading = false;
    }
  }
</script>

<FormDialog bind:open title="Create Thing" {loading} {error} onsubmit={handleSubmit}>
  <!-- form fields here -->
</FormDialog>
```

## Reusable Components

Always check these before building new UI:

| Component | Import | Use for |
|---|---|---|
| `SaveButton` | `$lib/components/ui/save-button.svelte` | Settings form save (dirty + loading) |
| `FormDialog` | `$lib/components/ui/form-dialog.svelte` | Create/edit dialogs with form + error |
| `CopyableInput` | `$lib/components/ui/copyable-input.svelte` | Readonly value with copy button, optional secret |
| `CorsEditor` | `$lib/components/ui/cors-editor.svelte` | CORS XML editing with save/delete |
| `IpListEditor` | `$lib/components/ui/ip-list-editor.svelte` | IP allow/deny lists with badges |
| `TagInput` | `$lib/components/ui/tag-input.svelte` | Tag/chip editing with add/remove |
| `ErrorBanner` | `$lib/components/ui/error-banner.svelte` | Error message display |
| `StatCard` | `$lib/components/ui/stat-card.svelte` | Metric display (bytes, counts) |
| `StorageProgressBar` | `$lib/components/ui/storage-progress-bar.svelte` | Quota utilization bar |
| `CardSkeleton` | `$lib/components/ui/skeleton/card-skeleton.svelte` | Loading placeholder for cards |
| `BulkDeleteDialog` | `$lib/components/ui/bulk-delete-dialog.svelte` | Multi-item delete confirmation |
| `DeleteConfirmDialog` | `$lib/components/ui/delete-confirm-dialog.svelte` | Single-item delete confirmation |
| `PageHeader` | `$lib/components/ui/page-header.svelte` | Page title + description + actions |
| `BackButton` | `$lib/components/ui/back-button.svelte` | Navigation back link |

## Utilities

| Utility | Import | Use for |
|---|---|---|
| `useSave()` | `$lib/utils/use-save.svelte.js` | Settings save pattern (saving + syncVersion + toast) |
| `useDelete()` | `$lib/utils/use-delete.svelte.js` | Delete with confirmation + toast |
| `formatBytes()` | `$lib/utils/format.ts` | Human-readable byte sizes |
| `parseQuotaBytes()` | `$lib/utils/format.ts` | Parse "50 GB" to number |
| `calcQuotaPercent()` | `$lib/utils/format.ts` | Percentage for quota bars |

## Section Component Pattern

Namespace/settings pages use section components inside a grid:

```svelte
<!-- +page.svelte -->
<div class="grid gap-6 lg:grid-cols-2">
  <SectionA {tenant} {namespaceName} />
  <SectionB {tenant} {namespaceName} />
</div>
```

Each section is a self-contained Card that:
1. Fetches its own data via `$derived(get_something(...))`
2. Shows a loading skeleton via `{#await data}` block
3. Manages its own local state and save logic
4. Uses `Card.Root > Card.Header > Card.Content > Card.Footer` structure

## Svelte 5 Rules

- **Runes only** — no `$:`, no `export let`, no `on:click`, no `<slot>`
- `$state` for mutable reactive variables
- `$derived` for computed values (NOT `$effect` for derivations)
- `$effect` only for syncing from server or external side effects
- `$props()` with TypeScript types for component props
- `$bindable()` for two-way bound props (e.g., `open` in dialogs)
- `{#snippet}` + `{@render}` instead of slots
- `onclick={handler}` not `on:click={handler}`
- `page.data.tenant` from `$app/state` (NOT `$app/stores`)

## Storybook

Visual component testing with **Storybook 10** + **Svelte CSF v5**.

**Commands:** `make storybook` (dev on port 6006) / `make build-storybook` (static build)

**CI:** `.github/workflows/storybook.yml` builds on PRs. On `main`, `docs.yml` builds both Zensical docs and Storybook, deploying to GitHub Pages at `/storybook/`.

**Actions panel (Svelte 5):** Svelte 5 uses callback props instead of `on:event`. Use `fn()` from `storybook/test` in `args` for Actions panel logging:
```svelte
import { fn } from 'storybook/test';
const { Story } = defineMeta({
  component: MyButton,
  args: { onclick: fn() },
});
```

**Running interaction tests in Storybook:**
1. `make storybook` → open `http://localhost:6006`
2. Sidebar → **Tests > DataTable Interactions** (or any story with play functions)
3. Bottom panel → click **"Interactions"** tab to see step-by-step results
4. Click the **play button** (triangle) in the Interactions panel to re-run tests
5. Each step shows pass/fail — click a step to inspect its state

**Story file convention:**
- Co-locate stories next to their component: `button.stories.svelte` beside `button.svelte`
- Stories live in `src/` (required for `$lib/` alias resolution) but are excluded from production builds
- Use Svelte CSF format with `defineMeta` from `@storybook/addon-svelte-csf`

**Storybook philosophy:**
- We test **our own custom components** — not third-party libraries
- shadcn-svelte components (Button, Badge, etc.) are already tested upstream — do NOT write stories for them unless we've significantly customized their behavior
- Stories focus on components with **our business logic and composition** (e.g., DataTable, CorsEditor, TagInput, FormDialog)
- If a component is just a thin wrapper around a shadcn-svelte primitive with no custom logic, skip the story

**When to add visual stories (`.stories.svelte`):**
- New reusable UI component **with custom logic** → add key variants
- Modified component props/behavior → update existing story
- Pure display components (progress bars, badges, stat cards) → visual story only, no interaction tests needed

**When to add interaction tests (`-interactions.stories.ts`):**
- Component has **user input** (typing, clicking, form submission)
- Component has **state transitions** (dirty tracking, loading states, open/close)
- Component has **validation logic** (duplicates, required fields, format checks)
- Do NOT write interaction tests for pure display components

**Story template:**
```svelte
<script module>
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import MyComponent from './my-component.svelte';

  const { Story } = defineMeta({
    title: 'UI/MyComponent',
    component: MyComponent,
  });
</script>

<Story name="Default" args={{ someProp: 'value' }}>
  {#snippet template(args)}
    <MyComponent {...args} />
  {/snippet}
</Story>
```

**For data-table stories:** use `createSvelteTable` with inline mock data, `renderComponent`/`renderSnippet` for cells, and `toast` for action feedback instead of real API calls.

### Mocking SvelteKit modules

Use `parameters.sveltekit_experimental` to mock SvelteKit imports in stories. This is needed when a component uses `$app/state`, `$app/navigation`, or `$app/forms`.

```svelte
<Story
  name="WithPageData"
  parameters={{
    sveltekit_experimental: {
      state: {
        page: {
          data: { tenant: 'test-tenant' },
        },
      },
    },
  }}
/>
```

Available mocks:
- `state.page` — mock `$app/state` page data (use this, NOT `stores.page`)
- `state.navigating` — mock navigation state
- `state.updated` — mock `{ current: boolean }` for update checks
- `navigation.goto` / `navigation.invalidate` — mock `$app/navigation` functions (defaults to Actions panel logging)
- `hrefs` — mock link click behavior: `{ '/path': (to, event) => { ... } }` (supports `asRegex: true`)
- `forms.enhance` — mock `$app/forms` enhance callback

### Interaction Tests (play functions)

For automated UI assertions (click, type, assert), use **CSF3 `.stories.ts`** files with play functions. The Svelte CSF addon does not support play functions, so interaction tests are written in TypeScript CSF3 format alongside the `.stories.svelte` visual stories.

**Pattern:** create a test harness `.svelte` component with all mock data inline (no props), then write CSF3 stories that target it:

```typescript
// my-component-interactions.stories.ts
import type { Meta, StoryObj } from '@storybook/svelte';
import { expect, userEvent, within } from 'storybook/test';
import TestHarness from './my-component-test-harness.svelte';

const meta = {
  title: 'Tests/MyComponent',
  component: TestHarness,
} satisfies Meta<TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FiltersCorrectly: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText('Search...');
    await userEvent.type(input, 'query');
    await expect(canvas.getByText('expected-result')).toBeInTheDocument();
  },
};
```

**Key conventions:**
- Test harness files: `*-test-harness.svelte` — self-contained, no props, inline mock data
- Interaction story files: `*-interactions.stories.ts` — CSF3 format with play functions
- Import `expect`, `userEvent`, `within`, `fn` from `storybook/test`
- Use `within(canvasElement)` to scope queries to the story canvas
- See `data-table-interactions.stories.ts` for a full example with search, selection, sorting, and bulk actions

## Naming Conventions

- Remote files: `*.remote.ts` (e.g., `namespaces.remote.ts`)
- Section components: `ns-*.svelte`, `settings-*.svelte`, `user-*.svelte`
- Dialog components: `*-dialog.svelte` (e.g., `bucket-create-dialog.svelte`)
- Utilities: `use-*.svelte.ts` for composables with reactive state
- Storybook visual stories: `*.stories.svelte` (Svelte CSF, co-located with component)
- Storybook interaction tests: `*-interactions.stories.ts` (CSF3 with play functions)
- Test harnesses: `*-test-harness.svelte` (self-contained wrapper for interaction tests)
- Roles enum: `ADMINISTRATOR`, `SECURITY`, `MONITOR`, `COMPLIANCE`

## Full-Stack Feature Checklist

When adding a new feature that touches the API, **all three layers must be updated**:

1. **Backend endpoint** (`backend/app/api/v1/endpoints/`) — the FastAPI route
2. **Mock server** (`backend/mock_server/mapi_state.py` + `fixtures.py`) — so the frontend can be tested locally
3. **Frontend remote function** (`frontend/src/lib/*.remote.ts`) — the `query()` or `command()` call
4. **Frontend UI** — the component(s) using the remote function
5. **Tests** (`backend/tests/`) — pytest tests for the endpoint

The mock server is what the frontend develops against. If a backend endpoint
exists but the mock doesn't handle it, the frontend cannot be tested.

## Required Skills

When working on frontend code, **always activate these skills**:

- `svelte:svelte-core-bestpractices` — Svelte 5 reactivity, events, styling
- `svelte:svelte-code-writer` — documentation lookup + autofixer validation
- `deno-skills:deno-expert` — Deno runtime best practices
- **Svelte MCP server** (`@sveltejs/mcp`) — use `svelte-autofixer` tool to validate components, `get-documentation` for syntax questions

These ensure code follows Svelte 5 runes patterns and Deno conventions.
Run the Svelte autofixer (`mcp__plugin_svelte_svelte__svelte-autofixer`) on any new or modified `.svelte` file before finalizing.
Use the shadcn-svelte MCP tools (`shadcnSvelteGetTool`, `shadcnSvelteSearchTool`) when looking up component APIs.

## Anti-Patterns to Avoid

- **Don't duplicate save boilerplate** — use `useSave()` composable
- **Don't duplicate dialog boilerplate** — use `FormDialog` component
- **Don't import `toast` directly** in settings forms — `useSave` handles it
- **Don't use `$effect` to compute derived values** — use `$derived`
- **Don't fetch same data in multiple sibling components** — lift query to parent
- **Don't use `Record<string, unknown>`** for typed API responses — define types
- **Don't use `$app/stores`** — use `$app/state` for page data
