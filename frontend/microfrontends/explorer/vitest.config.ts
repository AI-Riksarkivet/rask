import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	// Runes in `.svelte.ts` modules are compiled by the Svelte compiler, not by esbuild, so without this
	// plugin importing one fails at load with "$state is not defined" — the store never constructs and the
	// suite reports "0 test" rather than a failure, which reads like a pass. Needed to unit-test
	// `service-health.svelte.ts`; the zone's other tests import plain `.ts` and never hit it.
	plugins: [svelte()],
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node',
		// zod 3.25 re-exports `z` as a namespace (`import * as z; export { z }`);
		// vitest's runner mishandles that unless zod is transformed through Vite.
		server: { deps: { inline: ['valibot'] } },
	},
	resolve: {
		alias: { $lib: new URL('./src/lib', import.meta.url).pathname },
	},
});
