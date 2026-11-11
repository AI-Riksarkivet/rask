import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Used by Storybook to render components
export default defineConfig({
	plugins: [tailwindcss(), svelte()],
	test: {
		// BOTH homes: the package's suites live in `tests/`, and a few sit beside their source in
		// `src/`. Narrowing to one silently drops the other — 117 tests became 10 when this was
		// first written as src-only, which is exactly the failure mode a test config must not have.
		include: ['tests/**/*.{test,spec}.{js,ts}', 'src/**/*.{test,spec}.{js,ts}'],
		exclude: ['.svelte-kit/**', 'node_modules/**'],
	},
});
