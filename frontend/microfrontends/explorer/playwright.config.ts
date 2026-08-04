import { defineConfig, devices } from '@playwright/test';
import { E2E_ORIGIN, E2E_PORT, MOCK_SERVICES, MOCK_SERVICES_PORT } from './e2e/ports';

// Hermetic e2e for the media zone (search · descriptor boot · the zone-based BFF paths · the send half
// of the annotation funnel). The dev server runs the real SSR + hooks under the `/explorer` base (no OIDC
// env → the login gate is inactive).
//
// TWO kinds of stand-in, and which one a read gets is decided by WHERE the read runs:
//
//   - reads the BROWSER makes are mocked with `page.route`, scoped to the ZONE base
//     (`**/explorer/api/**` — a bare `**/api/**` glob also catches Vite /@fs module URLs like
//     …/packages/api/… and kills hydration);
//   - reads the ZONE SERVER makes cannot be reached that way at all. `/api/search` builds its Arrow
//     body in the route, and the projects plane moved into `projects.remote.ts` — so those get a mock
//     UPSTREAM (`e2e/mock-media-services.ts`) that the dev server is pointed at, and the real route or
//     remote function runs against it.
//
// The live-stack scripts (e2e/*.e2e.mjs, `test:e2e:live`) don't match the spec pattern and stay untouched.
export default defineConfig({
	testDir: './e2e',
	// Cold-Vite headroom: the FIRST request compiles the whole SSR route graph (the data
	// zone solves this with a warmup project; this small suite just carries the margin).
	timeout: 60_000,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	// ONE worker, deliberately. The mock upstream's seed/ledger is GLOBAL — this suite runs auth-off, so
	// there is no bearer to key it by the way the lakehouse admin suite does — and two spec FILES running
	// in parallel means one file's `__mock/reset` lands between another's seed and its page load. That is
	// exactly what it looked like when it happened: the project picker rendered empty, once, on a retry
	// that then passed. A shared fixture wants a serial suite or a keyed one; this suite is eight tests.
	workers: 1,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		baseURL: E2E_ORIGIN,
		trace: 'on-first-retry',
	},
	webServer: [
		{
			// A dedicated e2e port (not the 5173 dev port) so a running dev server can't clash.
			command: `bun run dev --port ${E2E_PORT} --strictPort`,
			port: E2E_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			// The two upstreams this zone's SERVER talks to in the suite. ANNOTATOR_API is set as well as
			// ANNOTATOR_PROJECTS_API so the dev-seam fallback in `projects.remote.ts` cannot quietly reach
			// a real service on :8103 if the primary name is ever dropped.
			env: {
				SEARCH_API: MOCK_SERVICES,
				ANNOTATOR_PROJECTS_API: MOCK_SERVICES,
				ANNOTATOR_API: MOCK_SERVICES,
				// The catalog too, for `catalog.remote.ts`'s user-state reads. `/v1/*`, `/api/*` and
				// `/projects*` never collide, so one port stands in for three services.
				CATALOG_API: MOCK_SERVICES,
			},
		},
		{
			command: 'bun run e2e/mock-media-services.ts',
			port: MOCK_SERVICES_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
	],
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
