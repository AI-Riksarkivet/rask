---
name: shadcn-svelte-skill
description: |
  Build accessible, customizable UI components for SvelteKit 2 / Svelte 5 projects using shadcn-svelte, Tailwind CSS v4, Deno, Storybook, Lucide icons, LayerChart, and motion-sv.
  Use when creating component-based Svelte applications that need production-ready, styled UI elements.
  Also covers Skeleton UI and Melt UI for library selection, Storybook for component dev, LayerChart for data visualization, and Lucide for icons.
  Triggers: "add components", "UI components", "build UI", "install component", "create form", "create dialog",
  "svelte components", "shadcn-svelte", "skeleton ui", "melt ui", "storybook", "component story",
  "chart", "visualization", "layerchart", "bar chart", "line chart", "pie chart",
  "icon", "lucide", "add icon", "tailwind", "tailwind config", "theme", "dark mode",
  "animation", "motion", "motion-sv", "framer motion", "animate", "transition", "gesture", "whileHover", "exit animation"
---

# shadcn-svelte · SvelteKit 2 + Svelte 5 + Deno + Storybook

Guide for working with shadcn-svelte in a **Deno-based SvelteKit 2 / Svelte 5** project with **Storybook**, **Lucide icons**, **LayerChart**, **motion-sv**, and **Tailwind CSS v4**.

All examples use Svelte 5 runes (`$state`, `$props`, `$derived`, `$effect`), Deno as the runtime, and Tailwind CSS v4 with `@tailwindcss/vite`.

## Reference Files

This skill has detailed reference files. Read the relevant one before diving in:

| Reference | When to read |
|-----------|-------------|
| `references/tailwind-v4.md` | Tailwind CSS v4.x setup, `@theme`, CSS-first config, `@utility`, v3→v4 migration |
| `references/lucide-icons.md` | Icon usage with `@lucide/svelte`, direct imports, dynamic icons, accessibility |
| `references/layerchart.md` | Data visualization with LayerChart v2 (next), shadcn-svelte chart integration, tooltips, simplified charts |
| `references/storybook.md` | Svelte CSF v5 patterns, templates, play functions, SvelteKit mocking, MDX docs, configuration |
| `references/motion-sv.md` | Animations with motion-sv (Framer Motion port), gestures, layout animations, AnimatePresence, page transitions |

## When to Use

- Building component-based SvelteKit 2 applications with Svelte 5
- You need accessible, styled UI elements (buttons, forms, modals, dialogs, data tables, etc.)
- You want copy-paste components you fully own and customize
- You're running Deno (not Node) as your runtime
- You use Storybook for isolated component development
- You need charts/visualizations (→ see `references/layerchart.md`)
- You need icons (→ see `references/lucide-icons.md`)
- You need animations, gestures, or page transitions (→ see `references/motion-sv.md`)

## Svelte Component Library Ecosystem

| Library | Type | Best For | Learning Curve |
|---------|------|----------|----------------|
| **shadcn-svelte** | Copy-paste components | Full customization, TypeScript-first | Medium |
| **Skeleton UI** | Installable package | Rapid development, themes | Low |
| **Melt UI** | Headless primitives | Maximum accessibility control | High |

## Core Concepts

**shadcn-svelte** is copy-paste component infrastructure. Components live in `$lib/components/ui/` — you own and modify the code directly. Built on **Bits UI** for accessibility, **Tailwind CSS v4** for styling, and **Svelte 5 runes** for reactivity.

## Setup: SvelteKit 2 + Deno + Tailwind v4

### 1. Create Project

```bash
deno run -A npm:sv create my-app
cd my-app
```

### 2. Install Tailwind v4 + shadcn-svelte

```bash
deno add -D npm:tailwindcss npm:@tailwindcss/vite
deno run -A npm:shadcn-svelte@latest init
```

### 3. Configure Vite

```typescript
// vite.config.ts
import { defineConfig } from 'vite'
import { sveltekit } from '@sveltejs/kit/vite'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
})
```

### 4. CSS Entry Point (`src/app.css`)

```css
@import "tailwindcss";
```

No `tailwind.config.js` needed. See `references/tailwind-v4.md` for `@theme`, `@utility`, and advanced config.

### 5. Layout (`src/routes/+layout.svelte`)

```svelte
<script>
  import '../app.css'
  let { children } = $props();
</script>

{@render children()}
```

### 6. Deno Configuration (`deno.json`)

```jsonc
{
  "tasks": {
    "dev": "deno run -A npm:vite dev",
    "build": "deno run -A npm:vite build",
    "preview": "deno run -A npm:vite preview",
    "storybook": "deno run -A npm:storybook dev -p 6006",
    "build-storybook": "deno run -A npm:storybook build"
  },
  "imports": { "$lib/": "./src/lib/" },
  "compilerOptions": { "verbatimModuleSyntax": true },
  "nodeModulesDir": "auto"
}
```

> `"nodeModulesDir": "auto"` is required for shadcn-svelte and Storybook under Deno.

### Add Components

```bash
deno run -A npm:shadcn-svelte@latest add button card dialog
deno run -A npm:shadcn-svelte@latest add --all    # or add everything
deno run -A npm:shadcn-svelte@latest list          # see available
```

Components install to: `src/lib/components/ui/[component-name]/`

## Svelte 5 Migration Cheat Sheet

| Svelte 4 | Svelte 5 |
|----------|----------|
| `export let prop` | `let { prop } = $props()` |
| `let x = value` (reactive) | `let x = $state(value)` |
| `$: derived = x + 1` | `let derived = $derived(x + 1)` |
| `$: { sideEffect() }` | `$effect(() => { sideEffect() })` |
| `<slot />` | `{@render children()}` |
| `<slot name="x" />` | `{@render x()}` |
| `let:attrs` | `{#snippet children({ props })}` |
| `asChild let:builder` | `{#snippet child({ props })}` |
| `on:click={handler}` | `onclick={handler}` |
| `<script context="module">` | `<script module>` |
| `createEventDispatcher()` | Callback props |

### Snippets Replace Slots (Critical for shadcn-svelte)

```svelte
<!-- Svelte 4 (old) -->
<Dialog.Trigger asChild let:builder>
  <Button builders={[builder]}>Open</Button>
</Dialog.Trigger>

<!-- Svelte 5 (current) -->
<Dialog.Trigger>
  {#snippet child({ props })}
    <Button {...props}>Open</Button>
  {/snippet}
</Dialog.Trigger>
```

## Common Workflows

### Basic Button

```svelte
<script lang="ts">
  import { Button } from "$lib/components/ui/button";
</script>

<Button variant="default">Click me</Button>
<Button variant="destructive">Delete</Button>
<Button variant="outline">Outlined</Button>
```

### Form with Validation

```bash
deno run -A npm:shadcn-svelte@latest add form input label
deno add npm:sveltekit-superforms npm:zod
```

```svelte
<script lang="ts">
  import { superForm } from "sveltekit-superforms";
  import { zodClient } from "sveltekit-superforms/adapters";
  import { z } from "zod";
  import * as Form from "$lib/components/ui/form";
  import { Input } from "$lib/components/ui/input";
  import { Button } from "$lib/components/ui/button";

  let { data } = $props();

  const schema = z.object({
    email: z.string().email(),
    name: z.string().min(2),
  });

  const form = superForm(data.form, { validators: zodClient(schema) });
  const { form: formData, enhance } = form;
</script>

<form method="POST" use:enhance>
  <Form.Field {form} name="email">
    <Form.Control>
      {#snippet children({ props })}
        <Form.Label>Email</Form.Label>
        <Input {...props} type="email" bind:value={$formData.email} />
      {/snippet}
    </Form.Control>
    <Form.FieldErrors />
  </Form.Field>
  <Button type="submit">Submit</Button>
</form>
```

### Dialog (Svelte 5)

```svelte
<script lang="ts">
  import * as Dialog from "$lib/components/ui/dialog";
  import { Button } from "$lib/components/ui/button";
  let open = $state(false);
</script>

<Dialog.Root bind:open>
  <Dialog.Trigger>
    {#snippet child({ props })}
      <Button {...props}>Open</Button>
    {/snippet}
  </Dialog.Trigger>
  <Dialog.Content>
    <Dialog.Header>
      <Dialog.Title>Title</Dialog.Title>
      <Dialog.Description>Description</Dialog.Description>
    </Dialog.Header>
    <p>Content here</p>
    <Dialog.Footer>
      <Button onclick={() => (open = false)}>Close</Button>
    </Dialog.Footer>
  </Dialog.Content>
</Dialog.Root>
```

### Data Table with TanStack Table v8

```bash
deno run -A npm:shadcn-svelte@latest add table data-table button dropdown-menu checkbox input
deno add npm:@tanstack/table-core
```

Key patterns:

1. **Svelte 5 state**: `$state` + `get` accessors in `createSvelteTable`
2. **State updater**: `(updater) => state = typeof updater === "function" ? updater(state) : updater`
3. **Cell rendering**: `createRawSnippet` → `renderSnippet` for HTML; `renderComponent` for components
4. **Row models**: Add matching model per feature (`getPaginationRowModel`, `getSortedRowModel`, `getFilteredRowModel`)
5. **Filter inputs**: Bind both `oninput` and `onchange`

See the [shadcn-svelte data table docs](https://www.shadcn-svelte.com/docs/components/data-table) for complete examples.

## Storybook Integration

For the full reference including SvelteKit mocking, play functions, shared templates, MDX docs, and configuration options, see `references/storybook.md`.

### Setup

```bash
deno run -A npm:storybook@latest init --type sveltekit
```

### Configure for Tailwind (`.storybook/preview.ts`)

```typescript
import type { Preview } from '@storybook/svelte';
import '../src/app.css';  // Tailwind + theme variables

const preview: Preview = {
  parameters: {
    controls: { matchers: { color: /(background|color)$/i, date: /Date$/i } },
  },
};
export default preview;
```

### Svelte CSF v5 Story Format (`.stories.svelte`)

```svelte
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import Button from './button.svelte';

  const { Story } = defineMeta({
    component: Button,
    tags: ['autodocs'],
    args: { children: 'Click me' as any },
    argTypes: {
      variant: { control: 'select', options: ['default', 'destructive', 'outline', 'ghost'] },
      size: { control: 'select', options: ['default', 'sm', 'lg', 'icon'] },
    },
    render: template,  // Default template for all stories
  });
</script>

{#snippet template(args)}
  <Button {...args}>{args.children}</Button>
{/snippet}

<!-- Uses default template from `render: template` above -->
<Story name="Default" args={{ variant: 'default' }} />

<Story name="Destructive" args={{ variant: 'destructive' }} />

<!-- Override with a per-story template -->
<Story name="With Icon">
  {#snippet template(args)}
    <Button {...args}>❤️ Save</Button>
  {/snippet}
</Story>

<!-- Static story (no args reactivity) -->
<Story name="Static" asChild>
  <Button>I'm static</Button>
</Story>
```

### Key Svelte CSF v5 Patterns

- `<script module lang="ts">` not `<script context="module">`
- `defineMeta({ component, render: template })` — sets default template
- `{#snippet template(args)}` for dynamic stories (NOT `{#snippet children(args)}`)
- Static content goes directly inside `<Story>` (no snippet needed)
- `asChild` for static stories that don't consume args
- `tags: ['autodocs']` for auto-generated docs (replaces `autodocs` prop)
- `onclick` not `on:click` for Svelte 5 event handlers

## Project Structure

```
src/
├── lib/
│   ├── components/
│   │   ├── ui/              # shadcn-svelte components
│   │   │   ├── button/
│   │   │   │   ├── index.ts
│   │   │   │   ├── button.svelte
│   │   │   │   └── Button.stories.svelte
│   │   │   ├── chart/       # Chart wrapper (from shadcn add chart)
│   │   │   ├── dialog/
│   │   │   └── [...]
│   │   └── custom/
│   └── utils/
│       └── cn.ts
├── routes/
└── app.css
.storybook/
├── main.ts
└── preview.ts
deno.json
```

## Troubleshooting

**Deno package resolution:** Set `"nodeModulesDir": "auto"` in `deno.json`.

**shadcn CLI not detecting project:** `deno run -A npm:shadcn-svelte@latest init --yes`

**Storybook can't find components:** Verify `"$lib/": "./src/lib/"` in `deno.json` imports.

**Styling not applying:** Check `@tailwindcss/vite` is in `vite.config.ts` plugins and `src/app.css` has `@import "tailwindcss"`. For Storybook, verify `preview.ts` imports `../src/app.css`.

## Resources

- **shadcn-svelte**: https://www.shadcn-svelte.com/docs
- **Bits UI**: https://bits-ui.com
- **Svelte 5**: https://svelte.dev/docs/svelte
- **Tailwind CSS v4**: https://tailwindcss.com/docs — see also `references/tailwind-v4.md`
- **Lucide Icons**: https://lucide.dev — see also `references/lucide-icons.md`
- **LayerChart v2**: https://next.layerchart.com — see also `references/layerchart.md`
- **motion-sv**: https://motion-sv.com — see also `references/motion-sv.md`
- **Storybook Svelte**: https://storybook.js.org/docs/svelte/get-started
- **Deno**: https://docs.deno.com
