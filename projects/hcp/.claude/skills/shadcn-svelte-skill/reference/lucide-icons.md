# Lucide Icons Reference (Svelte 5)

Comprehensive reference for using Lucide icons in SvelteKit 2 / Svelte 5 projects with Deno.

**Package:** `@lucide/svelte` (Svelte 5 native) — 1700+ icons
**Docs:** https://lucide.dev/guide/packages/lucide-svelte

## Table of Contents

1. [Installation](#installation)
2. [Basic Usage](#basic-usage)
3. [Direct Path Imports (Recommended)](#direct-path-imports)
4. [Customization Props](#customization-props)
5. [With shadcn-svelte Components](#with-shadcn-svelte-components)
6. [Dynamic Icons](#dynamic-icons)
7. [Lucide Lab (Extra Icons)](#lucide-lab)
8. [Accessibility](#accessibility)
9. [Storybook Stories](#storybook-stories)
10. [Performance Tips](#performance-tips)

## Installation

For **Svelte 5** projects, use the `@lucide/svelte` package (not `lucide-svelte`):

```bash
deno add npm:@lucide/svelte
```

> `lucide-svelte` supports Svelte 3/4/5 but `@lucide/svelte` is optimized for Svelte 5. shadcn-svelte uses `@lucide/svelte`.

## Basic Usage

Each icon is an individual Svelte component:

```svelte
<script lang="ts">
  import { Home, Settings, Search, Bell } from '@lucide/svelte';
</script>

<Home />
<Settings />
<Search />
<Bell />
```

Icons render as inline SVGs, fully tree-shakable — only imported icons end up in your bundle.

## Direct Path Imports

For faster builds and optimal tree-shaking, import from the icon's direct path:

```svelte
<script lang="ts">
  import CircleAlert from '@lucide/svelte/icons/circle-alert';
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import Ellipsis from '@lucide/svelte/icons/ellipsis';
  import Heart from '@lucide/svelte/icons/heart';
</script>

<CircleAlert color="#ff3e98" />
<ArrowUpDown class="ms-2" />
```

This is the **recommended import style** for production — avoids loading the full icon index.

## Customization Props

All icons accept these props:

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `size` | `number \| string` | `24` | Width and height in px |
| `color` | `string` | `currentColor` | Stroke color |
| `strokeWidth` | `number \| string` | `2` | Stroke width |
| `absoluteStrokeWidth` | `boolean` | `false` | Fixed stroke width regardless of size |
| `class` | `string` | `""` | CSS classes (Tailwind works great) |

### Examples

```svelte
<script lang="ts">
  import { Heart, Star, AlertCircle } from '@lucide/svelte';
</script>

<!-- Size variants -->
<Heart size={16} />
<Heart size={24} />
<Heart size={32} />

<!-- Color -->
<Star color="gold" fill="gold" />
<AlertCircle color="red" />

<!-- Stroke width -->
<Heart strokeWidth={1} />
<Heart strokeWidth={3} />

<!-- Tailwind classes -->
<Heart class="w-4 h-4 text-red-500" />
<Star class="w-6 h-6 text-yellow-400 fill-yellow-400" />
<AlertCircle class="size-5 text-destructive" />
```

### Using `currentColor` with Tailwind

Since the default color is `currentColor`, Tailwind's `text-*` classes control icon color:

```svelte
<div class="text-blue-600">
  <Home class="size-5" />  <!-- Inherits blue -->
  <span>Home</span>
</div>
```

## With shadcn-svelte Components

### Button with Icon

```svelte
<script lang="ts">
  import { Button } from "$lib/components/ui/button";
  import Heart from '@lucide/svelte/icons/heart';
  import Loader2 from '@lucide/svelte/icons/loader-2';
</script>

<Button>
  <Heart class="size-4 mr-2" />
  Save
</Button>

<!-- Loading state -->
<Button disabled>
  <Loader2 class="size-4 mr-2 animate-spin" />
  Loading...
</Button>

<!-- Icon-only button -->
<Button variant="ghost" size="icon">
  <Heart class="size-4" />
  <span class="sr-only">Like</span>
</Button>
```

### Sortable Table Header

```svelte
<script lang="ts">
  import type { ComponentProps } from "svelte";
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import { Button } from "$lib/components/ui/button/index.js";

  let { variant = "ghost", ...restProps }: ComponentProps<typeof Button> = $props();
</script>

<Button {variant} {...restProps}>
  Email
  <ArrowUpDown class="ms-2 size-4" />
</Button>
```

### Dropdown Menu Actions

```svelte
<script lang="ts">
  import Ellipsis from '@lucide/svelte/icons/ellipsis';
  import Copy from '@lucide/svelte/icons/copy';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { Button } from "$lib/components/ui/button/index.js";
  import * as DropdownMenu from "$lib/components/ui/dropdown-menu/index.js";
</script>

<DropdownMenu.Root>
  <DropdownMenu.Trigger>
    {#snippet child({ props })}
      <Button {...props} variant="ghost" size="icon" class="size-8">
        <Ellipsis class="size-4" />
        <span class="sr-only">Actions</span>
      </Button>
    {/snippet}
  </DropdownMenu.Trigger>
  <DropdownMenu.Content>
    <DropdownMenu.Item>
      <Copy class="size-4 mr-2" /> Copy
    </DropdownMenu.Item>
    <DropdownMenu.Item>
      <Pencil class="size-4 mr-2" /> Edit
    </DropdownMenu.Item>
    <DropdownMenu.Separator />
    <DropdownMenu.Item class="text-destructive">
      <Trash2 class="size-4 mr-2" /> Delete
    </DropdownMenu.Item>
  </DropdownMenu.Content>
</DropdownMenu.Root>
```

### Navigation Menu

```svelte
<script lang="ts">
  import Home from '@lucide/svelte/icons/home';
  import FileText from '@lucide/svelte/icons/file-text';
  import Settings from '@lucide/svelte/icons/settings';
  import type { ComponentType } from 'svelte';

  const navItems: { label: string; href: string; icon: ComponentType }[] = [
    { label: 'Home', href: '/', icon: Home },
    { label: 'Documents', href: '/docs', icon: FileText },
    { label: 'Settings', href: '/settings', icon: Settings },
  ];
</script>

<nav class="flex flex-col gap-1">
  {#each navItems as item}
    {@const Icon = item.icon}
    <a href={item.href} class="flex items-center gap-2 px-3 py-2 rounded-md hover:bg-muted">
      <Icon class="size-4" />
      <span>{item.label}</span>
    </a>
  {/each}
</nav>
```

## Dynamic Icons

### By Component Reference (Recommended)

```svelte
<script lang="ts">
  import { Home, Library, Cog } from '@lucide/svelte';
  import type { ComponentType } from 'svelte';

  interface MenuItem {
    name: string;
    href: string;
    icon: ComponentType;
  }

  const menuItems: MenuItem[] = [
    { name: 'Home', href: '/', icon: Home },
    { name: 'Blog', href: '/blog', icon: Library },
    { name: 'Settings', href: '/settings', icon: Cog },
  ];
</script>

{#each menuItems as item}
  {@const Icon = item.icon}
  <a href={item.href}>
    <Icon class="size-4" />
    <span>{item.name}</span>
  </a>
{/each}
```

### Generic Icon Component (Use with Caution)

This imports ALL icons — avoid in production:

```svelte
<script lang="ts">
  import * as icons from '@lucide/svelte';
  let { name, ...props }: { name: string } & Record<string, any> = $props();
  const Icon = icons[name as keyof typeof icons];
</script>

<Icon {...props} />
```

> **Warning:** Defeats tree-shaking. Only use for prototyping or when icon names come from dynamic data.

## Lucide Lab

Extra icons not in the main library:

```bash
deno add npm:@lucide/lab
```

```svelte
<script lang="ts">
  import { Icon } from '@lucide/svelte';
  import { pear, sausage } from '@lucide/lab';
</script>

<Icon iconNode={pear} />
<Icon iconNode={sausage} color="red" />
```

## Accessibility

By default, icons are decorative and hidden from screen readers (`aria-hidden="true"`).

For meaningful icons (not paired with text), add accessible labels:

```svelte
<!-- Decorative (default) — screen readers skip it -->
<Heart class="size-4" />

<!-- Meaningful — needs a label -->
<Heart class="size-4" aria-hidden="false" role="img" aria-label="Favorites" />

<!-- Best pattern: visually hidden text -->
<button>
  <Heart class="size-4" />
  <span class="sr-only">Add to favorites</span>
</button>
```

## Storybook Stories

```svelte
<!-- src/lib/components/ui/button/ButtonWithIcon.stories.svelte -->
<script module lang="ts">
  import { defineMeta } from '@storybook/addon-svelte-csf';
  import { Button } from './index.js';
  import Heart from '@lucide/svelte/icons/heart';
  import Loader2 from '@lucide/svelte/icons/loader-2';
  import Download from '@lucide/svelte/icons/download';

  const { Story } = defineMeta({
    title: 'UI/Button/With Icons',
    component: Button,
    tags: ['autodocs'],
  });
</script>

<Story name="Icon Left">
  {#snippet template(args)}
    <Button {...args}><Heart class="size-4 mr-2" /> Save</Button>
  {/snippet}
</Story>

<Story name="Icon Right">
  {#snippet template(args)}
    <Button {...args}>Download <Download class="size-4 ml-2" /></Button>
  {/snippet}
</Story>

<Story name="Loading">
  {#snippet template(args)}
    <Button {...args} disabled><Loader2 class="size-4 mr-2 animate-spin" /> Loading...</Button>
  {/snippet}
</Story>

<Story name="Icon Only">
  {#snippet template(args)}
    <Button {...args} variant="outline" size="icon">
      <Heart class="size-4" />
      <span class="sr-only">Like</span>
    </Button>
  {/snippet}
</Story>
```

## Performance Tips

1. **Always use direct path imports** for production: `from '@lucide/svelte/icons/heart'` not `from '@lucide/svelte'`
2. **Never import `*`** from `@lucide/svelte` in production — defeats tree-shaking
3. **Use Tailwind's `size-*` utility** instead of the `size` prop for consistency: `class="size-4"` instead of `size={16}`
4. **Prefer `currentColor`** (the default) and control color via parent's `text-*` class
5. **Add `sr-only` text** for icon-only buttons for accessibility
