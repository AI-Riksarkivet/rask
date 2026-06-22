import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: {
		experimental: {
			async: true,
		},
	},
	kit: {
		// SSR server runtime (Bun), served behind the gateway (:8888).
		adapter: adapter(),
		// No SvelteKit server endpoints / form actions exist yet, so CSRF origin
		// checks are moot. When SSR forms/+server POSTs land behind the gateway,
		// add `csrf: { trustedOrigins: ['<gateway-origin>'] }` (the modern
		// replacement for the now-deprecated `csrf.checkOrigin: false`).
		experimental: {
			remoteFunctions: true,
		},
	},
};

export default config;
