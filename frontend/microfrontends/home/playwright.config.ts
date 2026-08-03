import { defineConfig, devices } from '@playwright/test';
import { AUTH_OFF, AUTH_OFF_PORT, AUTH_ON, AUTH_ON_PORT, MOCK_CATALOG_PORT } from './e2e/ports';

// Hermetic e2e for the home zone — the catch-all at the origin root ("/"), which owns /auth/* AND, since
// the 2026-08-03 ruling, the PROJECTS surface (a project is the top of the hierarchy, so the estate's
// project list, one project's overview and the flow that mints one are main-menu pages). The dev server
// runs the real SSR + hydration at the origin root (home omits a base path).
//
// TWO app servers, exactly as the lakehouse zone runs one per auth mode:
//   · auth-OFF (`e2e/*.spec.ts`) — no OIDC env, so the relocated login/logout routes can be driven to
//     their fail-safe no-op redirects without a live Dex and the landing's auth affordance is proven
//     absent.
//   · auth-ON  (`e2e/projects/*.spec.ts`) — the projects surface only exists for a signed-in caller:
//     the estate-admin create door, the membership gallery and the project overview all key off
//     `/v1/me`, and `/v1/me` needs a bearer. One zone cannot be auth-ON and auth-OFF at once.
//
// The auth-ON server's catalog is `e2e/projects/mock-catalog.ts`: this zone reads and writes the catalog
// SERVER-side (the layout's `fetchMe`, the `/capi` pass-through, the remote functions), where
// `page.route` cannot reach, so the upstream itself is stubbed and seeded per bearer. Its port — and
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
			},
		},
		{
			command: 'bun e2e/projects/mock-catalog.ts',
			port: MOCK_CATALOG_PORT,
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
			testMatch: /\.spec\.ts$/,
			testIgnore: /e2e\/projects\//,
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
	],
});
