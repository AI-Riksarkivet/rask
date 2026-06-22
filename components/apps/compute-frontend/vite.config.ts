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
		// Client-side fetches hit /api/* → the gateway (or a mock gateway).
		proxy: {
			'^/api(/.*)?$': { target: VIEWER_BACKEND, changeOrigin: true },
		},
	},
});
