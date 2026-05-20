import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const VIEWER_BACKEND = process.env.VIEWER_BACKEND ?? 'http://localhost:8888';
const RAY_DASHBOARD = process.env.RAY_DASHBOARD_URL ?? 'http://localhost:8265';

// Ray Dashboard's bundled JS calls these absolute paths — when we host the SPA
// at /ray-dashboard/ those still hit our origin, so we forward them through.
// They don't collide with any of our /api/* routes (which are all under
// /api/health, /api/volumes, /api/batches, /api/chunks, /api/ray).
const RAY_ABSOLUTE_PATHS = [
	'/api/v0',
	'/api/jobs',
	'/api/cluster_status',
	'/api/version',
	'/api/grafana_health',
	'/api/prometheus_health',
	'/api/authenticate',
	'/api/authentication_mode',
	'/logs',
];
const rayProxyEntries = Object.fromEntries(
	RAY_ABSOLUTE_PATHS.map((p) => [p, { target: RAY_DASHBOARD, changeOrigin: true, ws: true }]),
);

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: {
		noExternal: ['svelte-sonner', 'mode-watcher', 'svelte-toolbelt'],
	},
	server: {
		proxy: {
			// Ray paths first — vite picks the longest match, so these win over /api.
			'/ray-dashboard': {
				target: RAY_DASHBOARD,
				changeOrigin: true,
				ws: true,
				rewrite: (path) => path.replace(/^\/ray-dashboard/, ''),
			},
			...rayProxyEntries,
			// Match exactly `/api` or `/api/...` — NOT `/api-docs`, `/api_v2`, etc.
			// Vite's plain-string keys do prefix matching, which would forward
			// any SvelteKit route starting with `/api` to the backend (404 →
			// SPA fallback, breaking the page).
			'^/api(/.*)?$': { target: VIEWER_BACKEND, changeOrigin: true },
		},
		watch: {
			ignored: ['**/.venv/**'],
		},
	},
});
