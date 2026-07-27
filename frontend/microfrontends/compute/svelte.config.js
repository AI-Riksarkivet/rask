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
		// Project-first IA: the base carries the project segment (one project, "default",
		// for now) so the turbo proxy still gets a STATIC per-app asset prefix — which is
		// what lets the dev proxy route this app's /@vite + built assets. Multi-project
		// (dynamic base) is deliberately deferred; backward-compat is not a concern yet.
		paths: { base: '/compute' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
