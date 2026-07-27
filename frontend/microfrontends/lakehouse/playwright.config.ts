import { defineConfig, devices } from '@playwright/test';
import {
	AUTH_OFF,
	AUTH_OFF_PORT,
	AUTH_ON,
	AUTH_ON_PORT,
	MOCK_CATALOG_PORT,
	MOCK_LINEAGE,
	MOCK_LINEAGE_PORT,
} from './e2e/ports';

// Hermetic e2e for the whole lakehouse zone — all four areas (e2e/data, e2e/lineage, e2e/models,
// e2e/admin), which used to be four separate zones each running their own dev server and browser.
// Every `/capi/**` + `/api/**` response is mocked via page.route, so no live backend is needed: the dev
// server runs the real SSR + client hydration under this zone's `/lakehouse` base path, and the
// browser's backend calls are the only thing stubbed. Specs `goto` the base-prefixed routes
// (`/lakehouse/data/...`, `/lakehouse/admin/...`, …), so a hop between AREAS is now exercised as the
// soft navigation it became.
//
// TWO app servers, because the admin area's suite is the one that exercises the REAL login gate: it
// needs OIDC configured (auth ON) plus a mock catalog for the control-events feed's server-side
// query.live poll. The other three areas run against an auth-OFF server, exactly as they did before
// the merge — one zone cannot be auth-ON and auth-OFF at once, and that split predates this config.
// Net it is still two dev servers where there used to be four.
// The ports live in e2e/ports.ts, imported by the mock catalog and the specs too — see the note
// there. The mock catalog in particular needs a port NO other zone claims, because
// `reuseExistingServer` is on locally: if something else is already listening, playwright ADOPTS it
// as the catalog instead of starting the mock, and the admin suite silently drives a real server.

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	// Capped. `fullyParallel` with one worker per core (32 on this box) puts 32 browsers on ONE vite dev
	// server, and dev is where SvelteKit pays full price per request — every remote-function call goes
	// through the SSR module pipeline. It was already marginal (a cold baseline run failed 2 of 212 on
	// nothing but timing); adding a live stream per page — held open, and outliving the closed page by a
	// poll or two while the server notices — pushed it to 14, all of them "the navbar isn't there yet"
	// after 5s. Measured at the mock lineage service: ~22 cursor probes/second at peak, from page churn
	// no real user produces. The estate itself is nowhere near this: 32 concurrent users on a zone
	// replica is 6.4 probes/second against a BUILT server. So the cap is the honest fix — the suite was
	// measuring the dev server's throughput, not the product.
	//
	// And 8 is a BIG-BOX number. A GitHub runner has 2–4 cores, so eight browsers on one Vite dev server
	// there is the same starvation an order of magnitude worse — which is what it looked like: 20+ specs
	// failing together with `element(s) not found` after 5s, across files with nothing in common, while
	// the same specs passed 6/6 locally. The suite was measuring the runner, not the product.
	workers: process.env.CI ? 2 : 8,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: { baseURL: AUTH_OFF, trace: 'on-first-retry' },
	webServer: [
		{
			// Dedicated e2e ports (not the 5174 microfrontends dev port) so a running composition can't clash.
			command: `bun run dev --port ${AUTH_OFF_PORT} --strictPort`,
			port: AUTH_OFF_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			// The live cursor (`$lib/live/feeds.remote`) and the shell's run feed poll the LINEAGE service
			// from the SERVER, so `page.route` cannot stand in for them the way it does for every other
			// read. Point them at the mock lineage service below; without it the cursor never advances and
			// no spec could tell a live surface from a dead one.
			env: { LINEAGE_API: MOCK_LINEAGE },
		},
		{
			command: `bun run dev --port ${AUTH_ON_PORT} --strictPort`,
			port: AUTH_ON_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			env: {
				OIDC_ISSUER: 'http://dex.test/dex',
				OIDC_CLIENT_ID: 'lance-admin-e2e',
				OIDC_REDIRECT_URI: `${AUTH_ON}/auth/callback`,
				CATALOG_API: `http://localhost:${MOCK_CATALOG_PORT}`,
				// The DLQ panel's live cursor is the LINEAGE one, not the control feed (an outbox drains when
				// lineage advances), so the admin server needs the mock lineage service too.
				LINEAGE_API: MOCK_LINEAGE,
			},
		},
		{
			command: 'bun e2e/admin/mock-catalog.ts',
			port: MOCK_CATALOG_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
		{
			command: 'bun e2e/lineage/mock-lineage.ts',
			port: MOCK_LINEAGE_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
	],
	projects: [
		// Warmup compiles the heavy routes ONCE before the parallel suite: with fullyParallel on a big box
		// (~32 workers) a cold Vite cache (e.g. right after a reformat) makes the whole first wave of tests
		// starve behind the initial compile and time out at 30s in a bundle — flaky counts per run.
		{
			name: 'warmup',
			testMatch: /e2e\/(data|lineage|models)\/warmup\.setup\.ts/,
			use: { baseURL: AUTH_OFF },
		},
		{
			name: 'chromium',
			testIgnore: /e2e\/admin\//,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_OFF },
			dependencies: ['warmup'],
		},
		{
			// The admin area against the auth-ON server. That is a SECOND dev server and a second Vite
			// compile, so it gets nothing from the auth-off warmup and needs its own — the admin routes
			// are the heaviest in the zone (Svelte Flow) and the first spec to reach one on a cold cache
			// times out at 30s every time.
			name: 'warmup-admin',
			testMatch: /e2e\/admin\/warmup\.setup\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
		},
		{
			name: 'chromium-admin',
			testMatch: /e2e\/admin\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
			dependencies: ['warmup-admin'],
		},
	],
});
