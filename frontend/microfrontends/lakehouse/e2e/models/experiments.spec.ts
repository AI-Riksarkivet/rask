import { test, expect, type Route } from '@playwright/test';

// Hermetic /experiments coverage (#53): the page calls the /api/experiments BFF, stubbed here — no
// live GreptimeDB needed (same pattern as models.spec.ts). Guards the embedded training-dashboard
// render and its unavailable/offline states so the frontend can't regress silently.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const dashboard = {
	dashboard: 'Model Training — experiment metrics',
	source: 'GreptimeDB (OTLP)',
	panels: [
		{ key: 'runs', title: 'Training runs /s by model', series: [{ model: 'demo', value: 0.02 }] },
		{
			key: 'rows',
			title: 'Rows seen (latest run) by model',
			series: [
				{ model: 'demo', value: 8 },
				{ model: 'fraud', value: 12 },
			],
		},
		{
			key: 'features',
			title: 'Feature datasets used (latest run) by model',
			series: [{ model: 'demo', value: 1 }],
		},
	],
};

test('renders the embedded training dashboard from the BFF', async ({ page }) => {
	await page.route('**/api/experiments', (route) => json(route, dashboard));
	await page.goto('/lakehouse/models/experiments');
	await expect(page.getByRole('heading', { name: 'Experiments' })).toBeVisible();
	await expect(page.locator('.src')).toContainText('Model Training');
	await expect(page.locator('.src')).toContainText('GreptimeDB');
	// The three panels and a per-model bar row each render.
	await expect(page.locator('section h2')).toHaveCount(3);
	await expect(page.locator('.bars li .model', { hasText: 'fraud' })).toBeVisible();
	await expect(page.locator('.bars li .val', { hasText: '12' })).toBeVisible();
	// No credential ever reaches the browser (the BFF queries GreptimeDB server-side).
	await expect(page.locator('.page')).not.toContainText('password');
});

test('shows the sign-in state on a governed stack without a session (401)', async ({ page }) => {
	await page.route('**/api/experiments', (route) => json(route, { detail: 'sign in' }, 401));
	await page.goto('/lakehouse/models/experiments');
	await expect(page.locator('.empty')).toContainText('sign in');
});

test('shows the honest unavailable state when observability is off (501)', async ({ page }) => {
	await page.route('**/api/experiments', (route) =>
		json(route, { detail: 'requires GreptimeDB' }, 501),
	);
	await page.goto('/lakehouse/models/experiments');
	await expect(page.locator('.empty')).toContainText('observability stack');
});

test('shows the offline state when the metrics store is unreachable', async ({ page }) => {
	await page.route('**/api/experiments', (route) => json(route, { detail: 'upstream' }, 502));
	await page.goto('/lakehouse/models/experiments');
	await expect(page.locator('.empty')).toContainText('unreachable');
});
