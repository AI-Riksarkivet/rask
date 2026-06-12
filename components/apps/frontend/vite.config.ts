import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const VIEWER_BACKEND = process.env.VIEWER_BACKEND ?? 'http://localhost:8888';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: {
		noExternal: ['svelte-sonner', 'mode-watcher', 'svelte-toolbelt'],
	},
	server: {
		proxy: {
			// Everything /api goes to the viewer backend — including /api/serve/*,
			// which the backend reverse-proxies to the Ray dashboard. (The embedded
			// dashboard and its direct-to-dashboard proxy paths are gone.)
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
