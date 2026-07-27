import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const VIEWER_BACKEND = process.env.VIEWER_BACKEND ?? 'http://localhost:8888';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: {
		noExternal: ['svelte-sonner', 'mode-watcher'],
	},
	server: {
		// Bind the port declared in microfrontends.json — the :3024 composition proxy
		// routes by it. strictPort makes a clash fail loudly instead of silently
		// drifting to the next free port (which breaks the proxy's routing).
		port: 5178,
		strictPort: true,
		// Client-side fetches hit /api/* → the gateway (or a mock gateway).
		proxy: {
			'^/api(/.*)?$': { target: VIEWER_BACKEND, changeOrigin: true },
		},
	},
});
