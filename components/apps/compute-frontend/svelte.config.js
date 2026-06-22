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
		// The compute (Ray/cluster) microfrontend, served under /compute.
		paths: { base: '/compute' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
