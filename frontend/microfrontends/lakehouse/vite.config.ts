import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const LANCE_BACKEND = process.env.LANCE_BACKEND ?? 'http://localhost:8001';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	ssr: {
		// @rask/* are JIT-TS workspace packages (exports → ./src/*.ts, no build step). Vite
		// externalizes them from the SSR transform, which hands them to Node's ESM resolver —
		// and that cannot resolve their extensionless relative imports ('./gateway'), so every
		// SSR render 500s with ERR_MODULE_NOT_FOUND. Inline them instead.
		noExternal: ['svelte-sonner', 'mode-watcher', /^@rask\//],
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
