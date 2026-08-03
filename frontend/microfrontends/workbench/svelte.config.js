import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: { experimental: { async: true } },
	kit: {
		adapter: adapter(),
		// THE global workbench: ONE dock holding panels from any zone, with a sidebar of saved views.
		// Its own zone because a Svelte component only renders if it is IN the bundle — the panels come
		// from @rask/panels rather than from another zone. Being a zone is also being a PROCESS, which
		// is what keeps @rask/media-api's module-level base safe here.
		paths: { base: '/workbench' },
		experimental: { remoteFunctions: true },
	},
};

export default config;
