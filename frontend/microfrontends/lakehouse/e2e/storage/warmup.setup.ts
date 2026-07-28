import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks here — failed /api calls are irrelevant; goto resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	await page.goto('/lakehouse/catalog/storage').catch(() => {});
});
