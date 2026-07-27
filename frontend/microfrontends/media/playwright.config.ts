import { defineConfig, devices } from '@playwright/test';

// Hermetic e2e for the media zone (search · descriptor boot · the zone-based BFF paths).
// The dev server runs the real SSR + hooks under the `/media` base (no OIDC env → the
// login gate is inactive); every backend response is mocked via page.route, scoped to
// the ZONE base (`**/media/api/**` — a bare `**/api/**` glob also catches Vite /@fs
// module URLs like …/packages/api/… and kills hydration). The live-stack scripts
// (e2e/*.e2e.mjs, `test:e2e:live`) don't match the spec pattern and stay untouched.
export default defineConfig({
	testDir: './e2e',
	// Cold-Vite headroom: the FIRST request compiles the whole SSR route graph (the data
	// zone solves this with a warmup project; this 2-test suite just carries the margin).
	timeout: 60_000,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		// A dedicated e2e port (not the 5173 dev port) so a running dev server can't clash.
		baseURL: 'http://localhost:5297',
		trace: 'on-first-retry',
	},
	webServer: {
		command: 'bun run dev --port 5297 --strictPort',
		port: 5297,
		reuseExistingServer: !process.env.CI,
		timeout: 120_000,
	},
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
