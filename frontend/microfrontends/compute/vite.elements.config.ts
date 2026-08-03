import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

/**
 * The zone's CUSTOM-ELEMENT build — the panels compute lends to the global workbench
 * (open_workbench.md). A separate config on purpose: only `src/lib/elements/**` compiles with
 * `customElement: true` (the flag changes component output wholesale, so it must never leak into
 * the app build), and the result is a plain ES library, not a SvelteKit app.
 *
 * Output lands INSIDE the app build (`build/client/compute/elements/`), which the patched
 * svelte-adapter-bun already serves — the element script is just one more static asset of this
 * zone's deployment, at /compute/elements/compute-elements.js. Runs after `vite build` (see the
 * package build script); `emptyOutDir: false` keeps the app build intact.
 */
export default defineConfig({
	plugins: [
		svelte({
			// EVERY .svelte in this build compiles in customElement mode (nested, tag-less
			// components stay ordinary inner components; an `include` filter would leave nested
			// imports uncompiled — review finding).
			compilerOptions: { customElement: true },
		}),
	],
	// The graph chain (@xyflow/svelte's dev-tooling guards) reads process.env.NODE_ENV; a lib build
	// defines no `process`, so the whole element bundle died at module scope with
	// "ReferenceError: process is not defined" (observed in the cluster). Define it away.
	define: { 'process.env.NODE_ENV': JSON.stringify('production') },
	build: {
		lib: {
			entry: 'src/lib/elements/index.ts',
			formats: ['es'],
			fileName: () => 'compute-elements.js',
		},
		outDir: 'build/client/compute/elements',
		emptyOutDir: false,
	},
	resolve: {
		// The wrapper imports @rask/api (JIT TS, no build) — bundle it in; the element must be
		// self-contained on the wire.
		dedupe: ['svelte'],
	},
});
