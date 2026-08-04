import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	// Runes in `.svelte.ts` modules are compiled by the Svelte compiler, not by esbuild, so without
	// this plugin importing one fails at load with "$state is not defined" — and vitest reports a
	// failed SUITE rather than a failed assertion, which is easy to misread as an environment
	// problem instead of a missing plugin. `explorer/vitest.config.ts` hit this first and carries the
	// same note; this zone needed it to unit-test `annotator.svelte.ts` (the canvas controller).
	plugins: [svelte()],
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node',
		// valibot is ESM-only; inline it so vitest's runner transforms it through Vite.
		server: { deps: { inline: ['valibot'] } },
	},
	resolve: {
		alias: { $lib: new URL('./src/lib', import.meta.url).pathname },
	},
});
