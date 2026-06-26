import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
	testDir: './tests',
	use: {
		baseURL: process.env.RASK_E2E_BASE_URL ?? 'http://localhost',
		trace: 'on-first-retry',
		screenshot: 'only-on-failure',
	},
	reporter: [['list']],
	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
