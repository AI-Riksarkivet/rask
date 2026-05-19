# Storybook Reference (SvelteKit 2 + Svelte CSF v5 + Deno)

Comprehensive reference for Storybook in SvelteKit 2 / Svelte 5 projects using the Svelte CSF v5 addon.

**Framework:** `@storybook/sveltekit`
**Story format:** `@storybook/addon-svelte-csf` v5 (Svelte CSF)
**Docs:** https://storybook.js.org/docs/svelte/get-started

## Table of Contents

1. [Installation with Deno](#installation-with-deno)
2. [Configuration](#configuration)
3. [Svelte CSF v5 Story Format](#svelte-csf-v5-story-format)
4. [Template Patterns](#template-patterns)
5. [Writing Stories for shadcn-svelte](#writing-stories-for-shadcn-svelte)
6. [Play Functions and Testing](#play-functions-and-testing)
7. [SvelteKit Module Mocking](#sveltekit-module-mocking)
8. [MDX Documentation](#mdx-documentation)
9. [Export Names and Organization](#export-names-and-organization)
10. [TypeScript Patterns](#typescript-patterns)
11. [Configuration Options](#configuration-options)
12. [Migration from CSF v4](#migration-from-csf-v4)
13. [Troubleshooting](#troubleshooting)

## Installation with Deno

```bash
deno run -A npm:storybook@latest init --type sveltekit
```

This scaffolds `.storybook/` config and installs dependencies. If addons are missing:

```bash
deno run -A npx storybook@latest add @storybook/addon-svelte-csf
```

### Deno Tasks (`deno.json`)

```jsonc
{
  "tasks": {
    "storybook": "deno run -A npm:storybook dev -p 6006",
    "build-storybook": "deno run -A npm:storybook build"
  }
}
```

## Configuration

### `.storybook/main.ts`

```typescript
import type { StorybookConfig } from '@storybook/sveltekit';

const config: StorybookConfig = {
  stories: ['../src/**/*.mdx', '../src/**/*.stories.@(js|ts|svelte)'],
  addons: [
    '@storybook/addon-essentials',
    '@storybook/addon-svelte-csf',
  ],
  framework: {
    name: '@storybook/sveltekit',
    options: {},
  },
};

export default config;
```

### `.storybook/preview.ts`

```typescript
import type { Preview } from '@storybook/svelte';
import '../src/app.css';  // Tailwind + shadcn-svelte theme variables

const preview: Preview = {
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
  },
};

export default preview;
```

> Importing `../src/app.css` is essential for Tailwind classes and shadcn-svelte CSS variables to work in Storybook.

## Svelte CSF v5 Story Format

### Minimal Story

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import MyComponent from './MyComponent.svelte';

  const { Story } = defineMeta({
    component: MyComponent,
  });
</script>

<Story name="Default" />
```

### Story with Args and Controls

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import { fn } from 'storybook/test';
  import Button from './button.svelte';

  const onclickFn = fn().mockName('onclick');

  /**
   * JSDoc here becomes the component description in autodocs.
   */
  const { Story } = defineMeta({
    component: Button,
    tags: ['autodocs'],
    args: {
      onclick: onclickFn,
      children: 'Click me' as any,
    },
    argTypes: {
      variant: {
        control: { type: 'select' },
        options: ['default', 'destructive', 'outline', 'ghost'],
      },
      size: {
        control: { type: 'select' },
        options: ['default', 'sm', 'lg', 'icon'],
      },
      children: { control: 'text' },
    },
    render: template,
  });
</script>

{#snippet template(args)}
  <Button {...args}>{args.children}</Button>
{/snippet}

<Story name="Default" args={{ variant: 'default' }} />

<Story name="Destructive" args={{ variant: 'destructive' }} />

<Story name="Large" args={{ size: 'lg' }} />

<Story name="Small" args={{ size: 'sm' }} />
```

### Key Anatomy

| Element | Purpose |
|---------|---------|
| `<script module lang="ts">` | Module-level script (Svelte 5 syntax) |
| `defineMeta({ ... })` | Defines story metadata, returns `{ Story }` |
| `tags: ['autodocs']` | Enables automatic documentation page |
| `args` | Default values for controls |
| `argTypes` | Control configuration (type, options) |
| `render: template` | Sets the default template snippet |
| `{#snippet template(args)}` | Dynamic rendering with args reactivity |
| `<Story name="..." />` | Individual story definition |

## Template Patterns

Storybook Svelte CSF v5 has several ways to define templates. Choose based on your needs:

### 1. Default Template via `render`

Set a default snippet for all stories that don't specify their own:

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import MyComponent from './MyComponent.svelte';

  const { Story } = defineMeta({
    component: MyComponent,
    render: defaultTemplate,  // Reference the snippet
  });
</script>

{#snippet defaultTemplate(args)}
  <MyComponent {...args} />
{/snippet}

<!-- These all use defaultTemplate automatically -->
<Story name="Default" />
<Story name="With Args" args={{ color: 'blue' }} />
```

### 2. Per-Story Template

Override the default for a specific story:

```svelte
<Story name="Custom Layout">
  {#snippet template(args)}
    <div class="p-4 bg-muted rounded-lg">
      <MyComponent {...args} />
    </div>
  {/snippet}
</Story>
```

### 3. Shared Template (Reusable Snippet)

Define a snippet at root level and pass it to multiple stories:

```svelte
{#snippet cardLayout(args)}
  <div class="border rounded-lg p-6">
    <MyComponent {...args} />
  </div>
{/snippet}

<Story name="In Card A" template={cardLayout} args={{ title: 'A' }} />
<Story name="In Card B" template={cardLayout} args={{ title: 'B' }} />
```

### 4. Static Story (No Args)

Content placed directly in `<Story>` is static — it won't react to Controls changes:

```svelte
<Story name="Static Example">
  <h2>This is static</h2>
  <MyComponent title="Fixed" />
</Story>
```

### 5. Static with `asChild`

For stories that are entirely static and shouldn't be wrapped:

```svelte
<Story name="Raw HTML" asChild>
  <h1>This renders directly</h1>
  <MyComponent>Static content</MyComponent>
</Story>
```

### Template Decision Tree

1. Need args reactivity? → Use `{#snippet template(args)}`
2. Same template for most stories? → Set `render: snippetRef` in `defineMeta`
3. Sharing template across some stories? → Define root snippet, pass via `template` prop
4. Pure static? → Put content directly in `<Story>` or use `asChild`

## Writing Stories for shadcn-svelte

### Button

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import { fn } from 'storybook/test';
  import { Button } from '$lib/components/ui/button/index.js';

  const { Story } = defineMeta({
    component: Button,
    tags: ['autodocs'],
    args: {
      onclick: fn().mockName('onclick'),
      children: 'Button' as any,
    },
    argTypes: {
      variant: {
        control: 'select',
        options: ['default', 'destructive', 'outline', 'secondary', 'ghost', 'link'],
      },
      size: {
        control: 'select',
        options: ['default', 'sm', 'lg', 'icon'],
      },
      children: { control: 'text' },
    },
    render: template,
  });
</script>

{#snippet template(args)}
  <Button {...args}>{args.children}</Button>
{/snippet}

<Story name="Default" />
<Story name="Secondary" args={{ variant: 'secondary' }} />
<Story name="Destructive" args={{ variant: 'destructive' }} />
<Story name="Outline" args={{ variant: 'outline' }} />
<Story name="Ghost" args={{ variant: 'ghost' }} />
```

### Dialog

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import * as Dialog from '$lib/components/ui/dialog/index.js';
  import { Button } from '$lib/components/ui/button/index.js';

  const { Story } = defineMeta({
    title: 'UI/Dialog',
    tags: ['autodocs'],
  });
</script>

<Story name="Default">
  {#snippet template()}
    <Dialog.Root>
      <Dialog.Trigger>
        {#snippet child({ props })}
          <Button {...props}>Open Dialog</Button>
        {/snippet}
      </Dialog.Trigger>
      <Dialog.Content>
        <Dialog.Header>
          <Dialog.Title>Dialog Title</Dialog.Title>
          <Dialog.Description>Description text.</Dialog.Description>
        </Dialog.Header>
        <p>Content goes here.</p>
      </Dialog.Content>
    </Dialog.Root>
  {/snippet}
</Story>
```

### Component with Snippets (Slots Replacement)

For components that accept snippets (Svelte 5's replacement for slots):

```svelte
<script module lang="ts">
  import { defineMeta, type StoryContext } from '@storybook/addon-svelte-csf';
  import Layout from './layout.svelte';
  import type { ComponentProps } from 'svelte';
  import type { Merge } from 'type-fest';

  const { Story } = defineMeta({
    component: Layout,
    render: template,
    args: {
      header: 'Default Header',
      children: 'Main content',
    },
    argTypes: {
      header: { control: 'text' },
      footer: { control: 'text' },
      children: { control: 'text' },
    },
    tags: ['autodocs'],
  });

  // Type snippets as strings for Controls compatibility
  type Args = Merge<ComponentProps<typeof Layout>, {
    header: string;
    children: string;
    footer?: string;
  }>;
</script>

{#snippet template({ children, ...args }: Args)}
  <Layout {...args}>
    {#snippet header()}
      {args.header}
    {/snippet}
    {children}
    {#snippet footer()}
      {args.footer}
    {/snippet}
  </Layout>
{/snippet}

<Story name="Default" />
<Story name="With Footer" args={{ footer: 'Footer content' }} />
```

## Play Functions and Testing

Use play functions for interaction testing within stories:

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import { expect, within, userEvent } from 'storybook/test';
  import { Button } from '$lib/components/ui/button/index.js';

  const { Story } = defineMeta({
    component: Button,
    render: template,
  });
</script>

{#snippet template(args)}
  <Button {...args} data-testid="btn">{args.children ?? 'Click me'}</Button>
{/snippet}

<Story
  name="Click Test"
  play={async (context) => {
    const { canvasElement } = context;
    const canvas = within(canvasElement);
    const button = await canvas.findByTestId('btn');

    await userEvent.click(button);
    expect(button).toBeInTheDocument();
  }}
/>

<Story
  name="Text Content Test"
  args={{ children: 'Submit' }}
  play={async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const button = await canvas.findByText('Submit');
    expect(button).toBeInTheDocument();
  }}
/>
```

## SvelteKit Module Mocking

Storybook provides experimental mocking for SvelteKit modules via `parameters.sveltekit_experimental`:

### Mocking `$app/state`

```svelte
<Story
  name="With Page Data"
  parameters={{
    sveltekit_experimental: {
      state: {
        page: {
          data: {
            user: { name: 'Gabriel', role: 'admin' },
          },
        },
        navigating: null,
        updated: { current: false },
      },
    },
  }}
/>
```

### Mocking `$app/navigation`

```svelte
<Story
  name="With Navigation"
  parameters={{
    sveltekit_experimental: {
      navigation: {
        goto: (url) => console.log('goto:', url),
        invalidate: (url) => console.log('invalidate:', url),
        invalidateAll: () => console.log('invalidateAll'),
      },
    },
  }}
/>
```

### Mocking Links

```svelte
<Story
  name="With Link Handling"
  parameters={{
    sveltekit_experimental: {
      hrefs: {
        '/dashboard': (to, event) => { console.log('navigating to', to); },
        '/api/.*': {
          callback: (to, event) => { console.log('API call to', to); },
          asRegex: true,
        },
      },
    },
  }}
/>
```

### Mocking `$app/forms`

```svelte
<Story
  name="With Form Enhance"
  parameters={{
    sveltekit_experimental: {
      forms: {
        enhance: () => { console.log('form enhanced'); },
      },
    },
  }}
/>
```

### SvelteKit Module Support Matrix

| Module | Status |
|--------|--------|
| `$app/environment` | ✅ Supported (`version` always empty) |
| `$app/forms` | ⚠️ Experimental (mock via parameters) |
| `$app/navigation` | ⚠️ Experimental (mock via parameters) |
| `$app/paths` | ✅ Supported (SvelteKit 1.4+) |
| `$app/state` | ⚠️ Experimental (SvelteKit 2.12+, mock via parameters) |
| `$app/stores` | ⚠️ Experimental (mock via parameters) |
| `$env/static/public` | ✅ Supported |
| `$env/dynamic/public` | 🚧 Dev mode only |
| `$lib` | ✅ Supported |
| `@sveltejs/kit/*` | ✅ Supported |
| `$env/*/private` | ⛔ Not supported (server-side) |

## MDX Documentation

Import stories in MDX files:

```mdx
<!-- MyComponent.docs.mdx -->
import { Meta, Canvas } from '@storybook/addon-docs/blocks';
import * as ButtonStories from './Button.stories.svelte';

<Meta title="UI/Button" name="Documentation" />

# Button Component

The Button component is our primary interactive element.

## Default

<Canvas of={ButtonStories.Default} />

## Destructive

<Canvas of={ButtonStories.Destructive} />
```

Stories are referenced by their export name (derived from `name` prop or explicit `exportName`).

## Export Names and Organization

### Auto-Generated Export Names

The `name` prop is converted to PascalCase for the export:

| `name` prop | Export name |
|-------------|-------------|
| `"Default"` | `Default` |
| `"With Icon"` | `WithIcon` |
| `"Large Size"` | `LargeSize` |

### Explicit Export Names

```svelte
<Story name="Nice Display Name" exportName="CustomExport">
  Content
</Story>
```

Reference in MDX: `<Canvas of={Stories.CustomExport} />`

## TypeScript Patterns

### Typing Args for Snippet Components

When a component uses Svelte 5 snippets (like `children`, `header`, `footer`), they appear as `Snippet` types. For Controls, cast them to strings:

```typescript
import type { ComponentProps } from 'svelte';
import type { Merge } from 'type-fest';

type Args = Merge<ComponentProps<typeof Layout>, {
  header: string;     // Instead of Snippet
  children: string;   // Instead of Snippet
  footer?: string;    // Instead of Snippet | undefined
}>;
```

### Typed StoryContext

```svelte
<script module lang="ts">
  import { defineMeta, type StoryContext } from '@storybook/addon-svelte-csf';
</script>

{#snippet template(args: MyArgs, context: StoryContext<MyArgs>)}
  <MyComponent {...args} />
{/snippet}
```

## Configuration Options

### Framework Options (`.storybook/main.ts`)

```typescript
framework: {
  name: '@storybook/sveltekit',
  options: {
    docgen: true,  // Set false to disable auto-docs for better perf
  },
},
```

### Addon Options

```typescript
addons: [
  {
    name: '@storybook/addon-svelte-csf',
    options: {
      legacyTemplate: false,  // Set true for backward compat with Template component
    },
  },
],
```

> `legacyTemplate: true` has a performance overhead. Only use during migration.

## Migration from CSF v4

| v4 (old) | v5 (current) |
|----------|-------------|
| `<Meta title="..." component={C} />` | `defineMeta({ title: '...', component: C })` |
| `import { Meta, Story } from '...'` | `const { Story } = defineMeta({...})` |
| `<Template let:args>` | `{#snippet template(args)}` |
| `<Story name="X" autodocs />` | `tags: ['autodocs']` in `defineMeta` |
| `<script context="module">` | `<script module>` |
| `<Story name="X"><slot /></Story>` | `<Story name="X">{#snippet template(args)}...{/snippet}</Story>` |

## Troubleshooting

### Tailwind Classes Not Applying

Ensure `.storybook/preview.ts` imports your CSS:
```typescript
import '../src/app.css';
```

### Components Not Found

Verify `$lib` alias resolves. Check `deno.json`:
```jsonc
{ "imports": { "$lib/": "./src/lib/" } }
```

And ensure `nodeModulesDir: "auto"` is set.

### Storybook Build Slow

Disable docgen if not needed:
```typescript
framework: {
  name: '@storybook/sveltekit',
  options: { docgen: false },
},
```

### Controls Not Working

Make sure you're using `{#snippet template(args)}` (not `{#snippet children(args)}`). Static content inside `<Story>` won't react to Controls changes.

### Import Errors with shadcn-svelte

Use full paths with `/index.js`:
```typescript
import { Button } from '$lib/components/ui/button/index.js';
import * as Dialog from '$lib/components/ui/dialog/index.js';
```

### `fn()` Mock Not Working

Import from `storybook/test`:
```typescript
import { fn } from 'storybook/test';
```
