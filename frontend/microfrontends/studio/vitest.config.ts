import { defineConfig } from 'vitest/config';

// Pure-TS engine tests only (executor / fingerprint / persistence) — no Svelte
// compilation, no DOM. The .svelte/.svelte.ts layer is exercised by svelte-check
// and the zone-contract gates instead.
export default defineConfig({
	test: {
		include: ['src/lib/flows/**/*.test.ts'],
		environment: 'node',
	},
});
