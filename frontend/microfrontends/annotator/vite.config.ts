import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
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
