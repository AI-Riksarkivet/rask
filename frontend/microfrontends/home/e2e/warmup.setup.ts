import { test } from '@playwright/test';

// Compile-warm the dev server before the parallel suite. `fullyParallel` starts every spec at once
// against a cold Vite cache, and the first wave then starves behind the initial compile and times out
// at 30s — deterministically, whenever anything this zone imports changed. The lakehouse zone had the
// same gap; every zone with a parallel suite needs one of these.
// No mocks: a failing /capi call is irrelevant, `goto` resolving means Vite compiled the route.
test('warm the dev server routes', async ({ page }) => {
	test.setTimeout(180_000);
	for (const path of ['/', '/auth/login', '/auth/logout']) {
		await page.goto(path).catch(() => {});
	}
});
