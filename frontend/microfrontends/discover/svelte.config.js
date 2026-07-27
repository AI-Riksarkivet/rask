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
		// The discover (document search/browse/viewer) microfrontend, served under
		// /discover. Project-first IA: the base carries the project segment
		// (one project, "default", for now) so the turbo proxy gets a STATIC per-app
		// asset prefix (routes this app's /@vite + built assets in dev). Multi-project
		// (dynamic base) is deferred on purpose.
		paths: { base: '/discover' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
