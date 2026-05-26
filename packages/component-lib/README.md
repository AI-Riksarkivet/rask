# @your-repo/oxen

Svelte 5 component library. Bits UI primitives + Tailwind 4 + Storybook.

## Develop

```bash
bun run --cwd packages/oxen_componets dev         # svelte-package -w
bun run --cwd packages/oxen_componets storybook   # localhost:6006
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
		"@your-repo/oxen": "workspace:*",
		"bits-ui": "^2.18.0",
	},
}
```

```svelte
<script lang="ts">
	import { Button, Dialog, Card } from '@your-repo/oxen';
</script>
```

In the app's CSS:

```css
@import 'tailwindcss';
@source '../../../../packages/oxen_componets/dist';
@source '../../../../packages/oxen_componets/src';
@import '@your-repo/oxen/styles/tokens.css';
```

## Adding a component

1. `src/lib/components/<name>/<name>.svelte`
2. `<name>.stories.ts` (simple) or `<name>.stories.svelte` (composite, uses snippets)
3. `index.ts` barrel
4. Re-export from `src/lib/index.ts`
5. Add to `package.json` `exports` if you want a deep import path
