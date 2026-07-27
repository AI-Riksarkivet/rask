import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks here — failed /capi calls are irrelevant; goto resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	for (const path of [
		'/lakehouse/models',
		'/lakehouse/models/pipeline',
		'/lakehouse/models/experiments',
	]) {
		await page.goto(path).catch(() => {});
	}
});
