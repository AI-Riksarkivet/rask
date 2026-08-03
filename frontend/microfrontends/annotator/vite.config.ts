import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	// Unset in normal dev — this exists for the e2e run, which starts a SECOND dev server from this
	// same directory (the "a model runner is deployed" app server; runner presence is server env, so
	// it cannot be a browser stub). Two Vite servers sharing one project share `node_modules/.vite`:
	// both write the dependency-optimizer cache and bump the browser's dep hash, so whichever
	// optimizes second makes the other's in-flight modules 504. It only bites while that cache is
	// COLD — which is a fresh checkout, i.e. every CI run — and it surfaces as SSR 500s and panels
	// that never render, never as anything naming the cause.
	cacheDir: process.env.RASK_VITE_CACHE_DIR,
	server: {
		// Bind the port declared in microfrontends/home/microfrontends.json — the composition proxy
		// routes by it. This zone used to pass `--port 5176` in its `dev` script WITHOUT strictPort, which
		// is the port `models` binds with strictPort: under `turbo run dev` models won the race and the
		// annotator silently drifted to the next free port, so every /annotator link landed on models.
		port: 5177,
		strictPort: true,
	},
	// No /api dev proxy anymore: the zone's own BFF routes (src/routes/api/**) serve
	// `${base}/api/*` in dev and prod alike, defaulting to the three local lance-media
	// services (VIEWER_API :8101 · SEARCH_API :8102 · ANNOTATOR_API :8103).
});
