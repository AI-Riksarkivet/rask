import { existsSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';
import {
	AUTH_OFF,
	AUTH_OFF_PORT,
	AUTH_ON,
	AUTH_ON_PORT,
	MOCK_CATALOG_PORT,
	MOCK_LINEAGE,
	MOCK_LINEAGE_PORT,
	MOCK_OBS,
	MOCK_OBS_PORT,
} from './e2e/ports';

// Hermetic e2e for the whole lakehouse zone — its areas (e2e/catalog, e2e/lineage, e2e/admin), which
// used to be separate zones each running their own dev server and browser. (`e2e/models` left with
// the models area itself; see microfrontends/models/e2e.)
// Every `/capi/**` + `/api/**` response is mocked via page.route, so no live backend is needed: the dev
// server runs the real SSR + client hydration under this zone's `/lakehouse` base path, and the
// browser's backend calls are the only thing stubbed. Specs `goto` the base-prefixed routes
// (`/lakehouse/catalog/...`, `/lakehouse/admin/...`, …), so a hop between AREAS is now exercised as the
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

// The auth-OFF areas that ship a warmup, DERIVED from the tree rather than hand-listed. The old
// literal named `data` — a directory that has not existed since the area merge — and omitted
// `catalog`, so the heaviest auth-off area ran against exactly the cold Vite cache this project
// exists to warm, and the config reported nothing at all. A regex over a list of directory names
// cannot notice that one of them is gone; deriving it can. `admin` is excluded on purpose: it warms
// the auth-ON server in its own `warmup-admin` project below.
const E2E_DIR = resolve(dirname(fileURLToPath(import.meta.url)), 'e2e');
const WARMUP_AREAS = readdirSync(E2E_DIR, { withFileTypes: true })
	.filter((d) => d.isDirectory() && d.name !== 'admin')
	.map((d) => d.name)
	.filter((name) => existsSync(resolve(E2E_DIR, name, 'warmup.setup.ts')))
	.sort();
if (WARMUP_AREAS.length === 0) {
	throw new Error('no e2e/<area>/warmup.setup.ts found — the warmup project would collect nothing');
}
const WARMUP_MATCH = new RegExp(`e2e/(${WARMUP_AREAS.join('|')})/warmup\\.setup\\.ts`);

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
			// no spec could tell a live surface from a dead one. CATALOG_API likewise, since the
			// remote-function migration: this server has NO session, so its bearer is "" — an auth-off
			// spec seeds the mock's "" bucket (`__mock/seed` with `bearer: ""`), which only works because
			// exactly one auth-off spec (shell-responsive) does so; anything needing per-test isolation
			// belongs in the admin (auth-ON) project with per-test bearers.
			env: { LINEAGE_API: MOCK_LINEAGE, CATALOG_API: `http://localhost:${MOCK_CATALOG_PORT}` },
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
				// The two NON-catalog upstreams the ported admin remote functions reach — one combined
				// seed-driven mock (paths never collide). See e2e/admin/mock-observability.ts.
				//
				// MEDALLION_API was a third. Nothing in THIS zone ever read it: it arrived with the
				// models surfaces that were ported out to `/models`, and the only reader went with them
				// (and has since been deleted with `/models/pipeline`). Removed rather than kept as
				// harmless — an env pointing a mock at an upstream no code calls reads as coverage.
				GREPTIME_API: MOCK_OBS,
				// The GATEWAY, for the promotions surface. Pointed at the same seed-driven mock for the
				// reason the comment above gives: paths never collide, and this one is `/api/promotions/*`,
				// which nothing else here serves. It must be set even though `gatewayJSON` has a default —
				// that default is `localhost:8888`, a real port on a dev host, so an unset var would let a
				// spec silently read a LIVE gateway and call it hermetic.
				RASK_GATEWAY_URL: MOCK_OBS,
				NATS_MONITOR_API: MOCK_OBS,
				// The dead-subscription detector's expectation list is SERVER env now (the jetstream port
				// computes missing = expected − bound). Fixed here; the streams spec's fixtures either
				// satisfy it (bound consumers present) or violate it per test.
				JETSTREAM_EXPECTED_CONSUMERS: 'MEDALLION:raw-to-bronze,TRAINING:lance-ray',
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
			command: 'bun e2e/admin/mock-observability.ts',
			port: MOCK_OBS_PORT,
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
			testMatch: WARMUP_MATCH,
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
