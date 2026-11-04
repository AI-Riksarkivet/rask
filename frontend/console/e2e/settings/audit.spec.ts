import { test, expect } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// `/settings/audit` — PLATFORM-wide (the trail spans every tenant and takes no project). The trail is the
// `lance.audit` log in the platform's telemetry store, which no lakehouse door serves and the console,
// having no backend, cannot query: the page stays (the owner's decision, XC-192) and says where the
// trail lives instead of rendering an empty table that would read as "nothing happened".

test.beforeEach(async ({ context, page }, testInfo) => {
	const token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	// The estate-admin identity the `/settings` layout door reads, seeded on the mock catalog.
	await seedFor(page, token, { 'GET /management/v1/me': ME_ADMIN });
});

test('the audit page says where the trail lives instead of rendering an empty one', async ({
	page,
}) => {
	await page.goto('/settings/audit');
	await expect(page.getByText("lives in the platform's telemetry store")).toBeVisible();
	await expect(page.getByRole('row')).toHaveCount(0);
});

test('the MAIN MENU stays on, and Settings is the entry that lights', async ({ page }) => {
	// `/settings/audit` is the ESTATE level: the bar is Home · Projects · Settings, and Settings lights
	// on the child route, not only on `/settings`.
	await page.goto('/settings/audit');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav.getByRole('link', { name: 'Home', exact: true })).toBeVisible();
	await expect(nav.getByRole('link', { name: 'Projects', exact: true })).toBeVisible();
	await expect(nav.getByRole('button', { name: 'Settings' })).toHaveAttribute('data-active', '');
	await expect(nav.getByRole('button', { name: 'Lakehouse' })).toHaveCount(0);
});
