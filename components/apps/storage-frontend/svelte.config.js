import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: {
		experimental: { async: true },
	},
	kit: {
		adapter: adapter(),
		// This microfrontend is served under /storage. Turborepo's MFE proxy routes
		// /storage/* here in dev; the gateway/K8s ingress does it in prod.
		// kit.paths.base is the SvelteKit-correct base path (NOT raw vite `base`).
		paths: { base: '/storage' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
