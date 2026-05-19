---
name: motion-sv
description: Documentation and patterns for using motion-sv, a Svelte 5 port of Motion (Framer Motion). Use this when the user wants animations, gestures, or transitions in Svelte.
---

# Motion for Svelte (motion-sv)

A port of the Motion library (formerly Framer Motion) specifically for Svelte 5. It aligns closely with the **motion-v** API structure rather than the React version.

## Stack Requirements

- MUST be used with Svelte 5 (Runes).
- Package name: `motion-sv`.

## Installation (Deno)

```bash
deno add npm:motion-sv
```

## Core Components

### `motion`

The primary component factory. Use dot notation to render **any** HTML element.

```svelte
<script>
  import { motion } from "motion-sv";
</script>

<!-- Sections & Headings -->
<motion.section initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
  <motion.h1 animate={{ y: 0 }}>Headline</motion.h1>
</motion.section>

<!-- Links & Buttons -->
<motion.a
  href="/about"
  whileHover={{ scale: 1.05 }}
  whilePress={{ scale: 0.95 }}
>
  Go to About
</motion.a>
```

### Styling Pattern (Important)

ALWAYS pass styles as an object via `style={{ key: value }}`, never as a string. This is required to bind `MotionValue`s correctly without triggering re-renders.

```svelte
<script>
  import { motion, useMotionValue } from "motion-sv";
  const x = useMotionValue(0);
</script>

<!-- ❌ BAD: String syntax (Values won't update) -->
<motion.div style="background-color: red; transform: translateX(10px)" />

<!-- ✅ GOOD: Object syntax -->
<motion.div
  style={{
    x,
    backgroundColor: "#ff0000",
    "--custom-var": 100,
  }}
/>
```

### Type Safety & Variants

For better developer experience and type safety, define variants using the `Variants` type.

```svelte
<script lang="ts">
  import { motion, type Variants } from "motion-sv";

  const boxVariants: Variants = {
    hidden: { opacity: 0, scale: 0.8 },
    visible: {
      opacity: 1,
      scale: 1,
      transition: { duration: 0.5 },
    },
  };
</script>

<motion.div variants={boxVariants} initial="hidden" animate="visible" />
```

### Supported Props

- **Animation:** `initial`, `animate`, `exit`, `transition`, `variants`.
- **Gestures:** `whileHover`, `whilePress` (preferred over `whileTap`), `whileDrag`, `whileFocus`.
- **Drag:** `drag` (boolean | "x" | "y"), `dragConstraints`, `dragElastic`, `dragMomentum`.
- **Events:** `onAnimationStart`, `onAnimationComplete`, `onUpdate`.
- **Gesture Events:** `onHoverStart`, `onHoverEnd`, `onPress`, `onPressStart`, `onPressEnd`.

### Viewport / Scroll Animations (Vue API Style)

**Important:** Use `inViewOptions` instead of the React `viewport` prop.

```svelte
<motion.section
  initial={{ opacity: 0 }}
  whileInView={{ opacity: 1 }}
  inViewOptions={{
    once: true,
    amount: "some", // "some" | "all" | 0..1
    margin: "0px 0px -200px 0px",
  }}
>
  Hello
</motion.section>
```

### `AnimatePresence`

Enables exit animations.
_Modes:_ `"sync"` (default), `"wait"`, `"popLayout"`.

```svelte
<script>
  import { motion, AnimatePresence } from "motion-sv";
  let show = $state(true);
</script>

<AnimatePresence mode="wait">
  {#if show}
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      key="unique-key"
    />
  {/if}
</AnimatePresence>
```

## Layout Animations (CRITICAL DIFFERENCE)

Svelte lacks `getSnapshotBeforeUpdate`. You **MUST** use `createLayoutMotion` for layout animations.

**Pattern:**

1. Create a proxy object: `const layout = createLayoutMotion(motion)`.
2. Use `<layout.div>` instead of `<motion.div>`.
3. Wrap state updates with `layout.update.with(fn)`.

```svelte
<script>
  import { motion, createLayoutMotion } from "motion-sv";

  let isOn = $state(false);
  const layout = createLayoutMotion(motion);

  // Wrap state mutation
  const toggle = layout.update.with(() => (isOn = !isOn));
</script>

<div onclick={toggle}>
  <!-- Use layout.div and layoutDependency or layoutId -->
  <layout.div
    layoutDependency={isOn}
    transition={{ type: "spring", stiffness: 700, damping: 30 }}
  />
</div>
```

## Reorder (Drag & Drop Lists)

Use specific components for reordering lists.

```svelte
<script>
  import { ReorderGroup, ReorderItem } from "motion-sv";
  let items = $state([0, 1, 2]);
</script>

<ReorderGroup axis="y" bind:values={items}>
  {#each items as item (item)}
    <ReorderItem value={item}>
      {item}
    </ReorderItem>
  {/each}
</ReorderGroup>
```

## Performance (Lazy Motion)

Reduce bundle size by loading features on demand.

```svelte
<script>
  import { LazyMotion, domAnimation } from "motion-sv";
</script>

<LazyMotion features={domAnimation}>
  <!-- Children using motion components -->
</LazyMotion>
```

## Usage with shadcn-svelte

### Animated Dialog

```svelte
<script lang="ts">
  import { motion, AnimatePresence } from "motion-sv";
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
  <AnimatePresence>
    {#if open}
      <Dialog.Content>
        <motion.div
          initial={{ opacity: 0, y: 20, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 20, scale: 0.95 }}
          transition={{ duration: 0.2 }}
        >
          <Dialog.Header>
            <Dialog.Title>Animated Dialog</Dialog.Title>
          </Dialog.Header>
          <p>Content with enter/exit animations.</p>
        </motion.div>
      </Dialog.Content>
    {/if}
  </AnimatePresence>
</Dialog.Root>
```

### Staggered List

```svelte
<script lang="ts">
  import { motion, type Variants } from "motion-sv";

  const container: Variants = {
    hidden: { opacity: 0 },
    show: {
      opacity: 1,
      transition: { staggerChildren: 0.1 },
    },
  };

  const item: Variants = {
    hidden: { opacity: 0, y: 20 },
    show: { opacity: 1, y: 0 },
  };

  let { items }: { items: string[] } = $props();
</script>

<motion.ul variants={container} initial="hidden" animate="show" class="space-y-2">
  {#each items as entry (entry)}
    <motion.li variants={item} class="rounded-lg border p-3">
      {entry}
    </motion.li>
  {/each}
</motion.ul>
```

### Animated Page Transitions

```svelte
<!-- src/routes/+layout.svelte -->
<script>
  import { motion, AnimatePresence } from "motion-sv";
  import { page } from "$app/state";
  import '../app.css';

  let { children } = $props();
</script>

<AnimatePresence mode="wait">
  {#key page.url.pathname}
    <motion.main
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      transition={{ duration: 0.2 }}
    >
      {@render children()}
    </motion.main>
  {/key}
</AnimatePresence>
```

## Best Practices

- **API Alignment:** Follow `motion-v` prop naming (e.g., `inViewOptions`, `whilePress`).
- **No Svelte transitions:** Do NOT use Svelte's `transition:fn` directives alongside motion-sv.
- **Styles:** Always use the object syntax `style={{ ... }}`.
- **Layout animations:** Always use `createLayoutMotion` — don't try `layout` prop directly.
- **Tailwind classes:** Regular `class` prop works fine on `motion.*` elements — use it for non-animated styles, and `style={{ }}` for animated values.
