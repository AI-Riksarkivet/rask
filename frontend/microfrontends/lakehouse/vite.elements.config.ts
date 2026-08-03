import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

/**
 * The zone's CUSTOM-ELEMENT build — the panels lakehouse lends to the global workbench
 * (open_workbench.md). A separate config on purpose: only `src/lib/elements/**` compiles with
 * `customElement: true` (the flag changes component output wholesale, so it must never leak into
 * the app build), and the result is a plain ES library, not a SvelteKit app.
 *
 * Output lands INSIDE the app build (`build/client/lakehouse/elements/`), which the patched
 * svelte-adapter-bun already serves — the element script is just one more static asset of this
 * zone's deployment, at /lakehouse/elements/lakehouse-elements.js. Runs after `vite build` (see the
 * package build script); `emptyOutDir: false` keeps the app build intact.
 */
export default defineConfig({
	plugins: [
		svelte({
			compilerOptions: { customElement: true },
			include: ['src/lib/elements/**/*.svelte'],
		}),
	],
	build: {
		lib: {
			entry: 'src/lib/elements/index.ts',
			formats: ['es'],
			fileName: () => 'lakehouse-elements.js',
		},
		outDir: 'build/client/lakehouse/elements',
		emptyOutDir: false,
	},
	resolve: {
		// The wrapper imports @rask/api (JIT TS, no build) — bundle it in; the element must be
		// self-contained on the wire.
		dedupe: ['svelte'],
	},
});
