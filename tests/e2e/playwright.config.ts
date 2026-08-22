import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests',
	use: {
		baseURL: process.env.RASK_E2E_BASE_URL ?? 'http://localhost',
		trace: 'on-first-retry',
		screenshot: 'only-on-failure',
		// THE ESCAPE HATCH THE SKIP MESSAGE OFFERS, WHICH DID NOT EXIST. `mfe.spec.ts` tells a reader
		// whose run bounced to auth to "give this suite a signed-in storageState" — and there was no
		// `storageState` and no `globalSetup` here, so the advice was unfollowable and the suite had no
		// way to run against the estate the chart ships (auth.enabled has defaulted to true since
		// 2026-08-06). Point RASK_E2E_STORAGE_STATE at a Playwright storage-state JSON:
		//   bunx playwright open --save-storage=state.json <issuer>   # sign in once
		//   RASK_E2E_STORAGE_STATE=state.json make e2e
		...(process.env.RASK_E2E_STORAGE_STATE ? { storageState: process.env.RASK_E2E_STORAGE_STATE } : {}),
	},
	reporter: [['list']],
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
