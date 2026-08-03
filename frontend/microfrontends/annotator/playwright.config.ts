import { defineConfig, devices } from '@playwright/test';
import {
	E2E,
	E2E_PORT,
	MOCK_ANNOTATOR,
	MOCK_ANNOTATOR_PORT,
	RUNNER_APP,
	RUNNER_PORT,
} from './e2e/ports';

// Hermetic e2e for the annotator zone (canvas-shell boot · the zone-based BFF paths · the projects
// plane). The dev server runs the real hooks under the `/annotator` base (no OIDC env → the login gate
// is inactive).
//
// TWO mocking layers, because the zone now speaks two transports:
//  · BYTES still ride `+server.ts` (the Arrow annotations plane, the media catch-all), so those are
//    mocked with `page.route`, scoped to the ZONE base (`**/annotator/api/**` — a bare `**/api/**`
//    glob also catches Vite /@fs module URLs like …/packages/api/… and kills hydration).
//  · VALUES ride remote functions since the transport ruling area 4, and those run on the zone SERVER
//    where `page.route` cannot see them. They are seeded on the mock ANNOTATOR service below, which
//    both app servers point at.
//
// TWO app servers, because the one thing the browser can no longer restub is the PRESENCE of a model
// runner: `zoneConfig` reads this pod's private env. The default server has no runner (the stack's
// honest-mock state, which most specs assert); the second has `MEDIA_ASSIST_URL` set, which is the
// only way to drive the real-runner assist path — and the only place the fail-honest chip test means
// anything, since with a runner deployed the chip is hidden unless the config read actually fails.

export default defineConfig({
	testDir: './e2e',
	// Cold-Vite headroom: the FIRST request compiles the whole route graph (the data
	// zone solves this with a warmup project; this suite just carries the margin).
	timeout: 60_000,
	// ONE worker, deliberately. The mock annotator's seed/ledger store is GLOBAL: with auth off every
	// request arrives without a bearer, so there is no per-test identity to key it by the way the
	// lakehouse admin suite does. Serialising the run is the cheap, honest guarantee that a spec only
	// ever sees the world it seeded — read the note in e2e/mock-annotator.ts before lifting it.
	workers: 1,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		// A dedicated e2e port (not the 5177 dev port) so a running dev server can't clash.
		baseURL: E2E,
		trace: 'on-first-retry',
	},
	webServer: [
		{
			command: `bun run dev --port ${E2E_PORT} --strictPort`,
			port: E2E_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			// The projects/tasks upstream the ported remote functions reach. No MEDIA_ASSIST_URL and no
			// MEDIA_JOBS_URL: this server IS the no-runner stack every honest-mock spec asserts.
			env: { ANNOTATOR_API: MOCK_ANNOTATOR, ANNOTATOR_PROJECTS_API: MOCK_ANNOTATOR },
		},
		{
			command: `bun run dev --port ${RUNNER_PORT} --strictPort`,
			port: RUNNER_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			env: {
				ANNOTATOR_API: MOCK_ANNOTATOR,
				ANNOTATOR_PROJECTS_API: MOCK_ANNOTATOR,
				// PRESENCE only — the web pod never calls this URL (the assist request goes to
				// ANNOTATOR_API). Setting it is exactly what "a model runner is deployed" means to
				// `zoneConfig`, which is the fact these specs need to be true.
				MEDIA_ASSIST_URL: 'http://runner.test',
				// Its OWN dependency-optimizer cache — see the note in vite.config.ts. Without this the
				// two servers fight over `node_modules/.vite` on a cold run.
				RASK_VITE_CACHE_DIR: 'node_modules/.vite-runner',
			},
		},
		{
			command: 'bun e2e/mock-annotator.ts',
			port: MOCK_ANNOTATOR_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
	],
	projects: [
		// Compiles BOTH servers' heavy routes, sequentially, before anything else runs — the cold-start
		// serialisation half of the two-server fix (see e2e/warmup.setup.ts and vite.config.ts).
		{
			name: 'warmup',
			testMatch: /e2e\/warmup\.setup\.ts/,
			timeout: 180_000,
			use: { ...devices['Desktop Chrome'], baseURL: E2E },
		},
		{
			name: 'chromium',
			testIgnore: /e2e\/runner\//,
			use: { ...devices['Desktop Chrome'], baseURL: E2E },
			dependencies: ['warmup'],
		},
		{
			name: 'chromium-runner',
			testMatch: /e2e\/runner\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: RUNNER_APP },
			dependencies: ['warmup'],
		},
	],
});
