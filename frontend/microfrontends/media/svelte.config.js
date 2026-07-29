import adapter from 'svelte-adapter-bun';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		// Enables `await` directly inside Svelte templates and load functions.
		// Useful for streaming search results progressively.
		experimental: {
			async: true,
		},
	},
	preprocess: vitePreprocess(),
	kit: {
		// Bun-server output — the rask MFE build target (`svelte-adapter-bun`).
		// SSR on (the estate zone contract): hooks.server.ts gates + hydrates the
		// session per request, and the zone's OWN BFF routes serve `${base}/api/*`
		// (src/routes/api/**) — client-only leaf pages opt out per-page (+page.ts).
		adapter: adapter(),
		// Zone base: this zone owns /media behind the lance ingress (same STATIC
		// per-app asset prefix as the other domain zones). The workspace-patched
		// svelte-adapter-bun serves assets at the based path too, so no
		// base-stripping proxy is needed in front of the built server.
		paths: { base: '/media', relative: false },
		// Remote functions: the zone's run-notification feed is a `query.live` generator
		// (`src/lib/live/feeds.remote.ts`). Without this the build fails outright rather than silently
		// dropping the endpoint, which is the right failure — see @rask/zone-contract's
		// notification-surface gate for why the bell must exist in every zone.
		experimental: { remoteFunctions: true },
		alias: {
			$lib: './src/lib',
			'$lib/*': './src/lib/*',
		},
	},
};

export default config;
