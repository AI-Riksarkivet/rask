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

test('the sidebar lights Settings and its Audit row on the child route', async ({ page }) => {
	await page.goto('/settings/audit');
	const nav = page.getByRole('navigation', { name: 'Pages' });
	await expect(nav.getByRole('link', { name: 'Settings', exact: true })).toHaveAttribute(
		'data-active',
		'true',
	);
	await expect(nav.getByRole('link', { name: 'Audit', exact: true })).toHaveAttribute(
		'data-active',
		'true',
	);
	await expect(nav.getByRole('link', { name: 'Home', exact: true })).not.toHaveAttribute(
		'data-active',
		'true',
	);
});
