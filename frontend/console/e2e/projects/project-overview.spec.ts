import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// `/projects/<p>` — ONE project's metadata and overview, a top-level page because a project is the
// TOP of the hierarchy. Its read is a `query` running in the browser, so the catalog answer is seeded
// on the mock rather than `page.route`d.
//
// The drill-down goes DOWN a rung at the project→warehouse step: every warehouse row links to its
// `/catalog/warehouses/<id>` page, which this file pins.

const DETAIL = {
	project: 'acme',
	warehouses: [
		{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' },
		{ id: 'acme-gold', bucket: 'gold-bucket', serving: 'gold', status: 'pending' },
	],
	admins: ['user:alice', 'role:7d2c0f4e-5b1a-4c3e-9f6d-2a8b1c0e9f3a#assignee'],
};

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, { 'GET /management/v1/me': ME_ADMIN });
});

test('renders the project’s warehouse rows and effective admins', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
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

	// The effective admins, verbatim — a userset (`role:<id>#assignee`) is a real grant and must not be
	// flattened into a plain user.
	await expect(page.getByText('user:alice', { exact: true })).toBeVisible();
	await expect(
		page.getByText('role:7d2c0f4e-5b1a-4c3e-9f6d-2a8b1c0e9f3a#assignee', { exact: true }),
	).toBeVisible();
});

test('each warehouse row links to its catalog page', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	for (const id of ['acme-wh', 'acme-gold']) {
		const link = page.getByRole('link', { name: id, exact: true });
		await expect(link).toHaveAttribute('href', `/catalog/warehouses/${id}`);
	}
	const back = page.locator('a.back');
	await expect(back).toHaveAttribute('href', '/projects');
});

test('a project the catalog refuses is named as denied, not rendered empty', async ({ page }) => {
	await seed(page, {
		'GET /management/v1/projects/secret': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/projects/secret');
	await expect(
		page.getByText("You don't have access to this project's registry facts."),
	).toBeVisible();
	// The honest failure, not a project that looks like it has nothing in it.
	await expect(page.getByRole('heading', { name: 'Warehouses' })).toHaveCount(0);
});
