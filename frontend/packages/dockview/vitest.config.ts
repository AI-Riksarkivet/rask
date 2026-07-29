import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

/**
 * `environment: 'node'` — matching every other vitest config in the estate (media, annotator,
 * zone-contract). That is a deliberate ceiling, not an oversight: there is NO DOM environment
 * anywhere in this repo (no jsdom, no happy-dom, no testing-library, no vitest-browser-svelte), so
 * these tests cover PURE LOGIC only.
 *
 * dockview is a layout engine that measures elements in pixels. Under jsdom every `offsetWidth` is 0,
 * so a "test" of a real `DockviewComponent` would exercise a grid that believes it has no size — a
 * proof that passes while telling you nothing. Anything touching a live `DockviewApi`, or whether a
 * control is actually visible and clickable, therefore belongs in Playwright against a real browser,
 * where the estate already runs its zone suites. Splitting it this way is why the modules under test
 * here are written to take their collaborators as arguments.
 *
 * The svelte plugin is present because runes in a `.svelte.ts` module are compiled by the Svelte
 * compiler, not esbuild: without it, importing one fails at load with "$state is not defined" and
 * vitest reports "0 tests" rather than a failure — which reads exactly like a pass.
 */
export default defineConfig({
	plugins: [svelte()],
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node',
	},
});
