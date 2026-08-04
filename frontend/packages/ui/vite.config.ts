import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Used by Storybook to render components
export default defineConfig({
	plugins: [tailwindcss(), svelte()],
	test: {
		// SOURCE ONLY. `svelte-package` copies every src file — test files included — into `dist/` and
		// `.svelte-kit/__package__`, and vitest's default discovery then collected all three copies, so
		// every test in this package ran THREE times against build output that is regenerated on each
		// build. Harmless while tests were pure, and immediately wrong for one that reads its own
		// source: the built copies have `index.js`, not `index.ts`.
		// BOTH homes: the package's suites live in `tests/`, and a few sit beside their source in
		// `src/`. Narrowing to one silently drops the other — 117 tests became 10 when this was
		// first written as src-only, which is exactly the failure mode a test config must not have.
		include: ['tests/**/*.{test,spec}.{js,ts}', 'src/**/*.{test,spec}.{js,ts}'],
		exclude: ['dist/**', '.svelte-kit/**', 'node_modules/**'],
	},
});
