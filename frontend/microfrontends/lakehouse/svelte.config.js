import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: { experimental: { async: true } },
	kit: {
		adapter: adapter(),
		// The merged estate zone (catalog + lineage + models + admin). A STATIC per-app asset prefix so the
		// routes this zone's built assets + /@vite in dev.
		paths: { base: '/lakehouse' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
