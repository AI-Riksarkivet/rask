import { defineConfig, devices } from '@playwright/test';
import {
	AUTH_OFF,
	AUTH_OFF_PORT,
	AUTH_ON,
	AUTH_ON_PORT,
	MOCK_CATALOG_PORT,
	MOCK_OBS_PORT,
	MOCK_NOTIFICATIONS_PORT,
} from './e2e/ports';

// Hermetic e2e for the home zone — the catch-all at the origin root ("/"), which owns /auth/* AND, since
// the 2026-08-03 ruling, the PROJECTS surface (a project is the top of the hierarchy, so the estate's
// project list, one project's overview and the flow that mints one are main-menu pages). The dev server
// runs the real SSR + hydration at the origin root (home omits a base path).
//
// TWO app servers, exactly as the lakehouse zone runs one per auth mode:
//   · auth-OFF (`e2e/*.spec.ts`) — no OIDC env, so the relocated login/logout routes can be driven to
//     their fail-safe no-op redirects without a live Dex and the landing's auth affordance is proven
//     absent.
//   · auth-ON  (`e2e/projects/*.spec.ts` and `e2e/settings/*.spec.ts`) — both surfaces only exist for a
//     signed-in caller: the estate-admin create door, the membership gallery, the project overview AND
//     the platform settings pages all key off `/v1/me`, and `/v1/me` needs a bearer. One zone cannot be
//     auth-ON and auth-OFF at once. Two playwright PROJECTS share that one server, because they share
//     its compile — a third dev server would double the cold-start for nothing.
//
// The auth-ON server's catalog is `e2e/mock-catalog.ts`: this zone reads and writes the catalog
// SERVER-side (the layout's `fetchMe`, the `/capi` pass-through, the remote functions), where
// `page.route` cannot reach, so the upstream itself is stubbed and seeded per bearer. `/settings/audit`
// reaches a SECOND upstream the same way — GreptimeDB, over `$lib/server/audit-core.ts` — so
// `e2e/mock-observability.ts` stands in for that one. Its port — and
// every port here — lives in e2e/ports.ts, because `reuseExistingServer` is on locally: if something
// else is already listening, playwright ADOPTS it as the catalog instead of starting the mock, and the
// suite silently drives a real server. `@rask/zone-contract` scans that file for collisions.
export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		baseURL: AUTH_OFF,
		trace: 'on-first-retry',
	},
	webServer: [
		{
			command: `bun run dev --port ${AUTH_OFF_PORT} --strictPort`,
			port: AUTH_OFF_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
		},
		{
			command: `bun run dev --port ${AUTH_ON_PORT} --strictPort`,
			port: AUTH_ON_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 120_000,
			env: {
				OIDC_ISSUER: 'http://dex.test/dex',
				OIDC_CLIENT_ID: 'lance-home-e2e',
				OIDC_REDIRECT_URI: `${AUTH_ON}/auth/callback`,
				// Both doors this zone opens on the catalog: the layout's `fetchMe` calls it directly and
				// the /capi pass-through + the projects remote functions forward the session bearer to it.
				CATALOG_API: `http://localhost:${MOCK_CATALOG_PORT}`,
				// The audit trail's SQL upstream. Absent, `audit-core.ts` answers 501 ("needs the
				// observability stack") — a real state the viewer renders, and not the one its spec means.
				GREPTIME_API: `http://localhost:${MOCK_OBS_PORT}`,
				// The watch page's remote functions call `${RASK_GATEWAY_URL}/api/notifications/*` from the
				// ZONE SERVER. Unset it points at :8888, which is unreachable here and answers `null` —
				// rendered as "watching is unavailable", indistinguishable from the bug this suite pins.
				RASK_GATEWAY_URL: `http://localhost:${MOCK_NOTIFICATIONS_PORT}`,
			},
		},
		{
			command: 'bun e2e/mock-catalog.ts',
			port: MOCK_CATALOG_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
		{
			command: 'bun e2e/mock-observability.ts',
			port: MOCK_OBS_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
		{
			command: 'bun e2e/mock-notifications.ts',
			port: MOCK_NOTIFICATIONS_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 30_000,
		},
	],
	projects: [
		// Compiles the routes ONCE before the parallel suite — see the warmup files. Each app server needs
		// its own: a second dev server is a second Vite compile and gets nothing from the first.
		{ name: 'warmup', testMatch: /e2e\/warmup\.setup\.ts/, use: { baseURL: AUTH_OFF } },
		{
			name: 'chromium',
			// AUTH-OFF owns the TOP LEVEL of e2e/ and nothing below it. Every subdirectory is an auth-ON
			// surface with its own project (projects/, settings/, notifications/), so the old
			// `testIgnore: /e2e\/(projects|settings)\//` had to be extended BY HAND for each new one —
			// and it was not. `e2e/notifications/watch-enrolment.spec.ts` was therefore collected here
			// too, against the auth-OFF server, where all 3 of its tests fail, while
			// `chromium-notifications` runs the same file correctly against the auth-ON one. Matching
			// only `e2e/<file>.spec.ts` states the RULE instead of listing its exceptions, so the next
			// subdirectory to land cannot repeat it.
			testMatch: /\/e2e\/[^/]+\.spec\.ts$/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_OFF },
			dependencies: ['warmup'],
		},
		{
			name: 'warmup-projects',
			testMatch: /e2e\/projects\/warmup\.setup\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
		},
		{
			name: 'chromium-projects',
			testMatch: /e2e\/projects\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
			dependencies: ['warmup-projects'],
		},
		// SETTINGS — the platform surfaces (#105). Same auth-ON server, its OWN warm-up: `/settings/access`
		// (Svelte Flow) and `/settings/audit` (the data table) are the two heaviest routes this zone
		// serves, and the projects warm-up never touches them.
		{
			name: 'warmup-settings',
			testMatch: /e2e\/settings\/warmup\.setup\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
		},
		{
			name: 'chromium-settings',
			testMatch: /e2e\/settings\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
			dependencies: ['warmup-settings'],
		},
		// NOTIFICATIONS — watch enrolment. Auth-ON like the two above and for the same reason: the page
		// lists the projects you may watch, that list is `me.projects`, and `/v1/me` needs a bearer.
		// It reuses the PROJECTS warm-up rather than adding a third: both compile the same auth-ON
		// server, and this route is a single card list — nothing like the Svelte Flow canvas that
		// earned settings its own.
		{
			name: 'chromium-notifications',
			testMatch: /e2e\/notifications\/.*\.spec\.ts/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
			dependencies: ['warmup-projects'],
		},
	],
});
