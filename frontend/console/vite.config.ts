import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { readFileSync } from 'node:fs';
import type { ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Connect, type Plugin, type ProxyOptions } from 'vite';

// The dev server's runtime config: `CONSOLE_CONFIG_FILE` when set, else the committed `dev/config.json`,
// which names the k3s estate's Dex at :8080 and the console's own public client. The preview server
// serves the built app as the image does, so it reads `CONSOLE_CONFIG_FILE` alone. Either way a config
// the console cannot use fails closed; sign-in is off only where the file says `{"signIn": false}`.
const DEV_CONFIG = fileURLToPath(new URL('./dev/config.json', import.meta.url));

/** The issuer's origin, when the config names an absolute issuer. */
function issuerOrigin(file: string): string | null {
	try {
		const issuer: unknown = (JSON.parse(readFileSync(file, 'utf8')) as { issuer?: unknown }).issuer;
		return typeof issuer === 'string' && URL.canParse(issuer) ? new URL(issuer).origin : null;
	} catch {
		return null;
	}
}

/** `connect-src` widened by exactly one origin, every other directive as SvelteKit wrote it. */
function allowConnect(policy: string, origin: string): string {
	return policy
		.split(';')
		.map((d) => d.trim())
		.filter(Boolean)
		.map((d) => (d.startsWith('connect-src ') ? `${d} ${origin}` : d))
		.join('; ');
}

function consoleConfig(): Plugin {
	const serveFrom =
		(file: string | undefined): Connect.NextHandleFunction =>
		(req, res, next) => {
			if (req.url !== '/config.json' || !file) return next();
			res.setHeader('content-type', 'application/json');
			res.setHeader('cache-control', 'no-store');
			res.end(readFileSync(file));
		};
	// Under `vite dev` SvelteKit sends the plugin's `csp` as a response HEADER (measured on kit 3.1.1: the page
	// answers `connect-src 'self'`), which would block the browser's discovery and token requests to
	// a cross-origin issuer. The page's policy gains that issuer's origin in `connect-src`, as `serve.ts`
	// does for the image; the built app's own policy is untouched.
	const allowIssuer =
		(file: string): Connect.NextHandleFunction =>
		(_req, res, next) => {
			const origin = issuerOrigin(file);
			if (!origin) return next();
			const setHeader = res.setHeader.bind(res);
			res.setHeader = (name: string, value: number | string | readonly string[]): ServerResponse =>
				setHeader(
					name,
					name.toLowerCase() === 'content-security-policy' && typeof value === 'string'
						? allowConnect(value, origin)
						: value,
				);
			next();
		};
	return {
		name: 'console-config',
		configureServer: (server) => {
			const file = process.env.CONSOLE_CONFIG_FILE || DEV_CONFIG;
			server.middlewares.use(serveFrom(file));
			server.middlewares.use(allowIssuer(file));
		},
		configurePreviewServer: (server) =>
			void server.middlewares.use(serveFrom(process.env.CONSOLE_CONFIG_FILE)),
	};
}

// Every `/api/*` call is same-origin. The dev server forwards them to the Gateway at `RASK_EDGE_URL`
// (`make dev-k3s` port-forwards k3s Traefik to :8080, which also serves the dev issuer at `/dex`) —
// the same routes a deployed console takes. (The Playwright suite runs the built app behind
// `e2e/edge.ts` instead.)
const EDGE = process.env.RASK_EDGE_URL ?? 'http://localhost:8080';
const proxy: Record<string, ProxyOptions> = { '/api': { target: EDGE, changeOrigin: true } };

export default defineConfig({
	plugins: [
		consoleConfig(),
		tailwindcss(),
		sveltekit({
			preprocess: vitePreprocess(),
			compilerOptions: { experimental: { async: true } },
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
		}),
	],
	server: { port: 5173, strictPort: true, proxy },
	preview: { port: 4173, strictPort: true, proxy },
});
