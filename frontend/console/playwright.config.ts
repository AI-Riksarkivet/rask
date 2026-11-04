import { defineConfig, devices } from '@playwright/test';
import {
	AUTH_OFF,
	AUTH_OFF_PORT,
	AUTH_ON,
	AUTH_ON_PORT,
	MOCK_CATALOG_PORT,
	MOCK_LINEAGE,
	MOCK_LINEAGE_PORT,
	MOCK_NOTIFICATIONS_PORT,
	MOCK_OIDC_PORT,
} from './e2e/ports';
import {
	AUTH_ON as ADMIN,
	AUTH_ON_PORT as ADMIN_PORT,
	MOCK_CATALOG as ADMIN_CATALOG,
	MOCK_CATALOG_PORT as ADMIN_CATALOG_PORT,
} from './e2e/admin/ports';

// Hermetic e2e for the console, against the BUILT app (`vite build` first; `test:e2e` does it) served
// by its own `serve.ts` behind `e2e/edge.ts`, which routes `/api/*` as the Gateway does. The console has
// no backend, so every call it makes is the browser's own: a spec stubs one with `page.route`, or the
// edge routes it to a seed-driven mock upstream.
//
// THREE origins of the one build, one per family of specs, because each family seeds its own mocks:
//   · auth-OFF (`e2e/*.spec.ts`, `e2e/catalog/`) — `e2e/console-config.off.json` says sign-in is off.
//   · auth-ON for the estate pages (`e2e/projects/`, `e2e/settings/`, `e2e/notifications/`).
//   · auth-ON for the lakehouse's pages (`e2e/admin/`).
// An auth-ON origin serves `e2e/console-config.json`, which names the mock issuer (`e2e/mock-oidc.ts`,
// same-origin under `/oidc`), so signing in runs the console's real PKCE round trip.
const CONFIG = './e2e/console-config.json';
const edge = 'bun e2e/edge.ts';

export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	workers: process.env.CI ? 2 : 8,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: { baseURL: AUTH_OFF, trace: 'on-first-retry' },
	webServer: [
		{
			command: edge,
			port: AUTH_OFF_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 60_000,
			env: {
				PORT: String(AUTH_OFF_PORT),
				CONSOLE_CONFIG_FILE: './e2e/console-config.off.json',
				CATALOG_API: ADMIN_CATALOG,
				LINEAGE_API: MOCK_LINEAGE,
			},
		},
		{
			command: edge,
			port: AUTH_ON_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 60_000,
			env: {
				PORT: String(AUTH_ON_PORT),
				CONSOLE_CONFIG_FILE: CONFIG,
				OIDC_MOCK: `http://localhost:${MOCK_OIDC_PORT}`,
				CATALOG_API: `http://localhost:${MOCK_CATALOG_PORT}`,
				LINEAGE_API: MOCK_LINEAGE,
				NOTIFICATIONS_API: `http://localhost:${MOCK_NOTIFICATIONS_PORT}`,
			},
		},
		{
			command: edge,
			port: ADMIN_PORT,
			reuseExistingServer: !process.env.CI,
			timeout: 60_000,
			env: {
				PORT: String(ADMIN_PORT),
				CONSOLE_CONFIG_FILE: CONFIG,
				OIDC_MOCK: `http://localhost:${MOCK_OIDC_PORT}`,
				CATALOG_API: ADMIN_CATALOG,
				LINEAGE_API: MOCK_LINEAGE,
			},
		},
		{ command: 'bun e2e/mock-oidc.ts', port: MOCK_OIDC_PORT, reuseExistingServer: !process.env.CI },
		{
			command: 'bun e2e/mock-catalog.ts',
			port: MOCK_CATALOG_PORT,
			reuseExistingServer: !process.env.CI,
		},
		{
			command: 'bun e2e/mock-notifications.ts',
			port: MOCK_NOTIFICATIONS_PORT,
			reuseExistingServer: !process.env.CI,
		},
		{
			command: 'bun e2e/lineage/mock-lineage.ts',
			port: MOCK_LINEAGE_PORT,
			reuseExistingServer: !process.env.CI,
		},
		{
			command: 'bun e2e/admin/mock-catalog.ts',
			port: ADMIN_CATALOG_PORT,
			reuseExistingServer: !process.env.CI,
		},
	],
	projects: [
		{
			name: 'auth-off',
			testMatch: [/\/e2e\/[^/]+\.spec\.ts$/, /\/e2e\/catalog\/.*\.spec\.ts$/],
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_OFF },
		},
		{
			name: 'estate',
			testMatch: /\/e2e\/(projects|settings|notifications)\/.*\.spec\.ts$/,
			use: { ...devices['Desktop Chrome'], baseURL: AUTH_ON },
		},
		{
			name: 'lakehouse',
			testMatch: /\/e2e\/admin\/.*\.spec\.ts$/,
			use: { ...devices['Desktop Chrome'], baseURL: ADMIN },
		},
	],
});
