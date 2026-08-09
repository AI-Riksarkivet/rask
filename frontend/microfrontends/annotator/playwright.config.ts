import { defineConfig, devices } from '@playwright/test';
import { E2E, E2E_PORT, MOCK_ANNOTATOR, MOCK_ANNOTATOR_PORT } from './e2e/ports';

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
// ONE app server. There used to be two: runner PRESENCE was read from the web pod's own env
// (`MEDIA_ASSIST_URL` → the deleted `zoneConfig` query), and no browser can restub server env, so
// driving the real-runner path meant standing up a whole second dev server with it set. Presence now
// comes from the annotator SERVICE — `GET /api/assist/producers` — which is seedable on the mock
// below like every other server-side value. The fact under test moved from deploy config to a
// per-test seed, and the second server, its own Vite cache dir, and the compile race between them
// all went with it.

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
			// The projects/tasks/assist upstream every ported remote function reaches. Whether a model
			// runner is deployed is no longer this server's env — it is whatever a spec seeds on the
			// mock, and unseeded means unreachable, which keeps the honest-mock chip up.
			// SEARCH_API points at the same generic mock: `findSimilar` is a remote function, so it
			// runs on the ZONE SERVER and `page.route` cannot see it — without this it fell through to
			// the `:8102` default, nothing answered, and the "more like this" panel could only ever be
			// driven into its error branch. That is why the propagation knobs (#87) had no browser
			// coverage at all: the controls render only once a search SUCCEEDS.
			env: {
				ANNOTATOR_API: MOCK_ANNOTATOR,
				ANNOTATOR_PROJECTS_API: MOCK_ANNOTATOR,
				SEARCH_API: MOCK_ANNOTATOR,
				// The corpus-rows by-key door (`fetchCorpusRows`) — the bulk grid's similarity view
				// needs the response's key_fields to associate search hits back onto rows.
				VIEWER_API: MOCK_ANNOTATOR,
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
		// Compiles the heavy routes before anything else runs, so no spec pays a ~20 s cold Vite
		// compile out of its own timeout (see e2e/warmup.setup.ts).
		{
			name: 'warmup',
			testMatch: /e2e\/warmup\.setup\.ts/,
			timeout: 180_000,
			use: { ...devices['Desktop Chrome'], baseURL: E2E },
		},
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'], baseURL: E2E },
			dependencies: ['warmup'],
		},
	],
});
