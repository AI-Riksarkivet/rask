import { defineConfig, devices } from '@playwright/test';

// Hermetic e2e for the home zone — the catch-all at the origin root ("/") that owns /auth/*. Runs auth-OFF
// (no OIDC env) so the relocated login/logout routes can be driven to their fail-safe no-op redirects over
// HTTP without a live Dex, and the landing's auth affordance is proven absent. The dev server runs the real
// SSR + hydration at the origin root (home omits a base path).
export default defineConfig({
	testDir: './e2e',
	timeout: 30_000,
	fullyParallel: true,
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	reporter: process.env.CI ? 'github' : 'list',
	use: {
		baseURL: 'http://localhost:5293',
		trace: 'on-first-retry',
	},
	webServer: {
		command: 'bun run dev --port 5293 --strictPort',
		port: 5293,
		reuseExistingServer: !process.env.CI,
		timeout: 120_000,
	},
	projects: [
		// Compiles the routes ONCE before the parallel suite — see e2e/warmup.setup.ts.
		{ name: 'warmup', testMatch: /warmup\.setup\.ts/ },
		{
			name: 'chromium',
			testMatch: /\.spec\.ts$/,
			use: { ...devices['Desktop Chrome'] },
			dependencies: ['warmup'],
		},
	],
});
