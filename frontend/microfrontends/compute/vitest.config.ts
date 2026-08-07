import { defineConfig } from 'vitest/config';

// Pure-TS unit tests only — no Svelte compilation, no DOM. The .svelte layer is exercised by
// svelte-check and the zone-contract gates instead, exactly as in `studio` and `explorer`.
//
// The zone had NO test runner until 2026-08-06, which meant a colocated `*.test.ts` would sit in the
// tree looking like a gate and never execute — the silent-green failure. It was added for
// `lib/remote/ingest-job.test.ts`, the regression on a run-board filter that could never match a row.
export default defineConfig({
	test: {
		include: ['src/lib/**/*.test.ts'],
		environment: 'node',
	},
});
