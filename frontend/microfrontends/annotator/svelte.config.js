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
		// Zone base: this zone owns /annotator behind the lance ingress (renamed
		// from the standalone repo's /annotate to match the zone-dir = base-path
		// convention). The workspace-patched svelte-adapter-bun serves assets at
		// the based path too (/annotator/_app/…), so no base-stripping proxy is
		// needed. `relative: false` keeps asset URLs absolute, trailing-slash-proof.
		paths: { base: '/annotator', relative: false },
		// Bun-server output — the rask MFE build target (`svelte-adapter-bun`).
		// SSR on (the estate zone contract): hooks.server.ts gates + hydrates the
		// session per request, and the zone's OWN BFF routes serve `${base}/api/*`
		// (src/routes/api/**) — the Pixi canvas page opts out per-page (+page.ts).
		adapter: adapter(),
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
