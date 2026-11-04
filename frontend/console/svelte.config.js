import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	compilerOptions: { experimental: { async: true } },
	kit: {
		// A single-page app with no backend of its own: every route renders in the browser from
		// `index.html`, which `serve.ts` (and any static server) answers for an unknown path.
		adapter: adapter({ fallback: 'index.html' }),
		// The content security policy travels with the app. `hash` puts the SHA-256 of SvelteKit's
		// inline bootstrap script into the page's own CSP meta tag; `serve.ts` sends the same policy as
		// a header, adding the issuer's origin to `connect-src` when Dex is served from another origin.
		csp: {
			mode: 'hash',
			directives: {
				'default-src': ['self'],
				'script-src': ['self'],
				// Svelte and Bits UI set inline `style` attributes (positioning, transitions).
				'style-src': ['self', 'unsafe-inline'],
				'img-src': ['self', 'data:', 'blob:'],
				'font-src': ['self', 'data:'],
				'connect-src': ['self'],
				'worker-src': ['self', 'blob:'],
				'object-src': ['none'],
				'base-uri': ['self'],
				'form-action': ['self'],
			},
		},
	},
};

export default config;
