# @rask/ui

Svelte 5 component library. Bits UI primitives + Tailwind 4 + Storybook.

## Develop

```bash
bun run --cwd packages/ui dev         # svelte-package -w
bun run --cwd packages/ui storybook   # localhost:6006
```

Or from the repo root:

```bash
bun run dev:ui
bun run storybook
```

## Consume from a SvelteKit app

```jsonc
{
	"dependencies": {
		"@rask/ui": "workspace:*",
		"bits-ui": "^2.18.0",
	},
}
```

```svelte
<script lang="ts">
	import { Button, Dialog, Card } from '@rask/ui';
</script>
```

In the app's CSS:

```css
@import 'tailwindcss';
@source '../../../../packages/ui/dist';
@source '../../../../packages/ui/src';
@import '@rask/ui/styles/tokens.css';
```

## Adding a component

1. `src/lib/components/<name>/<name>.svelte`
2. `<name>.stories.ts` (simple) or `<name>.stories.svelte` (composite, uses snippets)
3. `index.ts` barrel
4. Re-export from `src/lib/index.ts`
5. Add to `package.json` `exports` if you want a deep import path
