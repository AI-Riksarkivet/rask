import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const VIEWER_BACKEND = process.env.VIEWER_BACKEND ?? 'http://localhost:8888';
const LANCE_BACKEND = process.env.LANCE_BACKEND ?? 'http://localhost:8001';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: { noExternal: ['svelte-sonner', 'mode-watcher'] },
	server: {
		// 5179: the first free port after train's 5178. strictPort so a clash fails loudly rather than
		// drifting to another port, which would silently break the :3024 composition proxy's routing.
		port: 5179,
		strictPort: true,
		proxy: {
			'^/api/ray(/.*)?$': { target: VIEWER_BACKEND, changeOrigin: true },
			'^/api(/.*)?$': { target: LANCE_BACKEND, changeOrigin: true },
		},
	},
});
