import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { seed as seedFor } from './mock-client';

// `/projects/<p>` — ONE project's metadata and overview, moved here from the lakehouse zone with the
// list (2026-08-03 ruling: a project is the TOP of the hierarchy, so the page describing one is a
// top-level page). Its read is a remote `query` running on the zone server, so the catalog answer is
// seeded on the mock rather than `page.route`d.
//
// The load-bearing thing this file pins is the ZONE SEAM. The drill-down now crosses zones exactly
// once, at the project→warehouse rung: every warehouse link leaves home for `/lakehouse/...` and MUST
// carry `data-sveltekit-reload`, or SvelteKit soft-navigates into a route this zone's manifest does not
// own and 404s. The estate-wide gate (`@rask/zone-contract`'s cross-zone-reload suite) cannot see this
// direction of the seam — its ZONES list is every zone dir EXCEPT home — so without this test the
// attribute could be dropped and every run would still be green.

const DETAIL = {
	project: 'acme',
	warehouses: [
		{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' },
		{ id: 'acme-gold', bucket: 'gold-bucket', serving: 'gold', status: 'pending' },
	],
	admins: ['user:alice', 'team:eng#member'],
};

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, { 'GET /v1/me': ME_ADMIN });
});

test('renders the project’s warehouse rows and effective admins', async ({ page }) => {
	await seed(page, { 'GET /v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	await expect(page.getByRole('heading', { name: 'acme', exact: true })).toBeVisible();

	// Every registry fact the row carries — id, bucket and the status chip, whose non-active value is
	// the only thing separating a provisioned warehouse from one that is still coming up.
	const work = page.getByRole('row').filter({ hasText: 'acme-wh' });
	await expect(work).toContainText('acme-bucket');
	await expect(work).toContainText('active');
	const gold = page.getByRole('row').filter({ hasText: 'acme-gold' });
	await expect(gold).toContainText('gold-bucket');
	await expect(gold).toContainText('pending');

	// The effective admins, verbatim — a userset (`team:eng#member`) is a real grant and must not be
	// flattened into a plain user.
	await expect(page.getByText('user:alice', { exact: true })).toBeVisible();
	await expect(page.getByText('team:eng#member', { exact: true })).toBeVisible();
});

test('each warehouse row crosses INTO the lakehouse zone with data-sveltekit-reload', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	for (const id of ['acme-wh', 'acme-gold']) {
		const link = page.locator(`a[href="/lakehouse/catalog/warehouses/${id}"]`);
		await expect(link).toBeVisible();
		// A cross-zone <a> without this attribute soft-navigates into a route home does not own → 404.
		await expect(link).toHaveAttribute('data-sveltekit-reload', '');
	}
	// …and the way BACK is same-zone now, so it must NOT pay a document load. This is the half of the
	// seam the re-home created: `/projects` used to be `/lakehouse/catalog/projects`.
	const back = page.locator('a.back');
	await expect(back).toHaveAttribute('href', '/projects');
	expect(await back.getAttribute('data-sveltekit-reload')).toBeNull();
});

test('a project the catalog refuses is named as denied, not rendered empty', async ({ page }) => {
	await seed(page, { 'GET /v1/projects/secret': { status: 403, body: { detail: 'forbidden' } } });
	await page.goto('/projects/secret');
	await expect(
		page.getByText("You don't have access to this project's registry facts."),
	).toBeVisible();
	// The honest failure, not a project that looks like it has nothing in it.
	await expect(page.getByRole('heading', { name: 'Warehouses' })).toHaveCount(0);
});
