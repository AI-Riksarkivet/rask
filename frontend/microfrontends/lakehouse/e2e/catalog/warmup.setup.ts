import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks here — failed /capi calls are irrelevant; goto resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	for (const path of [
		'/lakehouse/catalog/tables',
		'/lakehouse/catalog/tables/db1%24t',
		'/lakehouse/catalog/namespaces',
		'/lakehouse/catalog/namespaces/gold',
		'/lakehouse/catalog/warehouses',
		'/lakehouse/catalog/warehouses/acme-wh',
		// No `/lakehouse/catalog/projects*` rows: both routes are DELETED (2026-08-03 ruling — a
		// project is the top of the hierarchy, so its list and its overview are the home zone's).
	]) {
		await page.goto(path).catch(() => {});
	}
});
