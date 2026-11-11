# @rask/ui

Svelte 5 component library. Bits UI primitives + Tailwind 4 + Storybook.

## Develop

```bash
bun run --cwd packages/ui storybook   # localhost:6006
```

There is no build step: the package's `exports` name `src/`, and the console compiles it from source,
so an edit reaches `bun --cwd=frontend run dev` through Vite's HMR.

## Consume from a SvelteKit app

```jsonc
{
	"dependencies": {
		"@rask/ui": "workspace:*",
		"bits-ui": "^2.18.0",
	},
}
```

Import from the per-component subpath exports (never the root barrel or a deep
`@rask/ui/src/...` path):

```svelte
<script lang="ts">
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Dialog } from '@rask/ui/dialog';
</script>
```

In the app's CSS:

```css
@import 'tailwindcss';
@import 'tw-animate-css';
@import '@rask/ui/styles/tokens.css';

/* Tailwind 4 skips node_modules — scan @rask/ui's source or its classes vanish. */
@source '../../packages/ui/src';
```

## Adding a component

1. `src/lib/components/<name>/<name>.svelte`
2. `<name>.stories.ts` (simple) or `<name>.stories.svelte` (composite, uses snippets)
3. `index.ts` barrel
4. Add the `./<name>` subpath to `package.json` `exports` — this is the
   canonical import path (`@rask/ui/<name>`); consumers import from the subpath.
5. Optionally re-export from `src/lib/index.ts` (the root `.` barrel is a
   curated convenience surface, not the full component set — subpaths are
   authoritative).
