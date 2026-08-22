import { defineConfig, devices } from '@playwright/test';
import { APP, APP_PORT, MOCK_UPSTREAMS, MOCK_UPSTREAMS_PORT } from './e2e/ports';

// Hermetic e2e for the MODELS zone — the registry and its experiments dashboard, which arrived here
// from `lakehouse/e2e` when the routes themselves moved out of `/lakehouse/models`.
//
// TWO servers, and the split is by WHO MAKES THE READ, not by area:
//
//  - the zone's own dev server, running the real SSR + hydration under this zone's `/models` base path;
//  - one seed-driven mock upstream (e2e/mock-upstreams.ts) standing in for the catalog, GreptimeDB and
//    the lineage probe. Every one of those reads is issued by a REMOTE FUNCTION on the zone server, so
//    `page.route` cannot reach it — this zone has no browser-side backend read left to mock, which is
//    why there is no page.route stand-in here at all.
//
// AUTH-ON, deliberately (see e2e/session.ts): the surfaces refuse without a session, and the per-test
// bearer is what keeps parallel specs from sharing the mock's fixtures. Every spec signs in before its
// first `goto` — the login-first gate would otherwise redirect to `/auth/login`, a home-zone route absent
// from this isolated server.
//
// The ports live in e2e/ports.ts, imported by the mock and the specs too — see the note there.

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	// Capped, on the lakehouse's measurement: `fullyParallel` with one worker per core (32 on this box)
	// puts 32 browsers on ONE vite dev server, and dev is where SvelteKit pays full price per request —
	// every remote-function call goes through the SSR module pipeline. A GitHub runner has 2–4 cores, so
	// the CI number is lower again; the suite must measure the product, not the runner.
	workers: process.env.CI ? 2 : 8,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: { baseURL: APP, trace: 'on-first-retry' },
	webServer: [
		{
			// A dedicated e2e port (not the 5178 microfrontends dev port) so a running composition can't clash.
			command: `bun run dev --port ${APP_PORT} --strictPort`,
			port: APP_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			env: {
				// Auth ON. Nothing ever reaches this issuer: the specs mint the session cookie directly
				// (no SESSION_SECRET → the documented dev-grade unsealed cookie), and the OIDC env exists
				// only to make `makeOidcConfig` return a config, which is what turns `locals.authEnabled` on.
				OIDC_ISSUER: 'http://dex.test/dex',
				OIDC_CLIENT_ID: 'models-e2e',
				OIDC_REDIRECT_URI: `${APP}/auth/callback`,
				// The three upstreams this zone's remote functions reach server-side, all on the one mock
				// (their paths never collide — see e2e/mock-upstreams.ts). MEDALLION_API left with the
				// `/pipeline` trigger door: nothing in this zone reads it any more, and a stand-in for an
				// upstream nobody calls is a mock that can only ever pass.
				CATALOG_API: MOCK_UPSTREAMS,
				GREPTIME_API: MOCK_UPSTREAMS,
				// The shell's notification bell probes lineage from the SERVER on a 5s clock. Pointed at the
				// mock so an unseeded probe is a fast 404 the feed fails quiet on, rather than a connection
				// refused to whatever happens to be on :8001 on the developer's box.
				LINEAGE_API: MOCK_UPSTREAMS,
			},
		},
		{
			command: 'bun e2e/mock-upstreams.ts',
			port: MOCK_UPSTREAMS_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
	],
	projects: [
		// Warmup compiles the zone's routes ONCE before the parallel suite. With `fullyParallel` on a big
		// box a cold Vite cache makes the whole first wave starve behind the initial compile and time out
		// at 30s in a bundle — a flaky count per run rather than a failure. Signed in, because the
		// login-first gate would otherwise redirect the warm-up navigation away from the route it means
		// to compile.
		{ name: 'warmup', testMatch: /e2e\/warmup\.setup\.ts/, use: { ...devices['Desktop Chrome'] } },
		{
			name: 'chromium',
			testMatch: /e2e\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'] },
			dependencies: ['warmup'],
		},
	],
});
