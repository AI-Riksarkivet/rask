import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { readFileSync } from 'node:fs';
import { defineConfig, type Connect, type Plugin, type ProxyOptions } from 'vite';

// `/config.json` is served from `CONSOLE_CONFIG_FILE` here exactly as `serve.ts` serves it in the
// image, so a dev or preview server signs in against whichever issuer that file names; without the
// file the app runs with sign-in off.
function consoleConfig(): Plugin {
	const serve: Connect.NextHandleFunction = (req, res, next) => {
		const file = process.env.CONSOLE_CONFIG_FILE;
		if (req.url !== '/config.json' || !file) return next();
		res.setHeader('content-type', 'application/json');
		res.setHeader('cache-control', 'no-store');
		res.end(readFileSync(file));
	};
	return {
		name: 'console-config',
		configureServer: (server) => void server.middlewares.use(serve),
		configurePreviewServer: (server) => void server.middlewares.use(serve),
	};
}

// Every `/api/*` call is same-origin. The dev server forwards them to the cluster's Gateway at
// `RASK_EDGE_URL` (`make dev-k3s` port-forwards it to :8888) — the same routes a deployed
// console takes. (The Playwright suite runs the built app behind `e2e/edge.ts` instead.)
const EDGE = process.env.RASK_EDGE_URL ?? 'http://localhost:8888';
const proxy: Record<string, ProxyOptions> = { '/api': { target: EDGE, changeOrigin: true } };

export default defineConfig({
	plugins: [consoleConfig(), tailwindcss(), sveltekit()],
	server: { port: 5173, strictPort: true, proxy },
	preview: { port: 4173, strictPort: true, proxy },
});
