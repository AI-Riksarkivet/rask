import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const LANCE_BACKEND = process.env.LANCE_BACKEND ?? 'http://localhost:8001';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: {
		noExternal: ['svelte-sonner', 'mode-watcher'],
	},
	server: {
		// Bind the port declared in microfrontends/home/microfrontends.json — the
		// composition proxy routes by it. strictPort fails loudly on a clash.
		port: 5174,
		strictPort: true,
		proxy: {
			'^/api(/.*)?$': { target: LANCE_BACKEND, changeOrigin: true },
		},
	},
});
