import { fileURLToPath } from 'node:url';
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
			// EVERY .svelte in this build compiles in customElement mode — including nested,
			// tag-less components (LineageGraph, its nodes) — which is what makes their scoped
			// styles INJECT at runtime instead of being extracted to a css asset nothing loads.
			// (The compiler leaves tag-less components as ordinary inner components; review
			// finding: an `include` filter here would leave nested imports uncompiled.)
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
			fileName: () => 'lakehouse-elements.js',
		},
		outDir: 'build/client/lakehouse/elements',
		emptyOutDir: false,
	},
	resolve: {
		// The wrapper imports @rask/api (JIT TS, no build) — bundle it in; the element must be
		// self-contained on the wire. `$lib` is SvelteKit's alias and does not exist in a plain
		// lib build, so it is re-declared for the lineage components this bundle carries.
		dedupe: ['svelte'],
		alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) },
	},
});
