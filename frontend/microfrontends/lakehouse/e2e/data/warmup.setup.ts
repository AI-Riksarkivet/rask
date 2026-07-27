import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks here — failed /capi calls are irrelevant; goto resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	for (const path of [
		'/lakehouse/data/tables',
		'/lakehouse/data/tables/db1%24t',
		'/lakehouse/data/namespaces',
		'/lakehouse/data/namespaces/gold',
		'/lakehouse/data/warehouses',
		'/lakehouse/data/warehouses/acme-wh',
		'/lakehouse/data/projects',
		'/lakehouse/data/projects/acme',
	]) {
		await page.goto(path).catch(() => {});
	}
});
