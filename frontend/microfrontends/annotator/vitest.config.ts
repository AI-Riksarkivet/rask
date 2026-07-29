import { defineConfig } from 'vitest/config';

export default defineConfig({
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
