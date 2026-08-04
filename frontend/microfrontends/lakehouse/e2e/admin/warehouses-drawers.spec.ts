import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Row-drawer coverage for the WAREHOUSE registry (goal cond 8): clicking a row opens the bits-ui
// drawer with the full record + jump links, and the in-cell link/action button stopPropagation so they
// keep their own behavior without also opening it. The table's read rides `warehouses.remote.ts` now,
// so the registry is seeded on the mock catalog per bearer instead of intercepted in the browser —
// which is why these two tests live here and not beside the table/namespace drawers in e2e/catalog.

const WAREHOUSES = [
	{
		id: 'acme-wh',
		project: 'acme',
		bucket: 'acme-bucket',
		root_uri: 's3://acme-bucket',
		status: 'active',
		created_at: '2026-07-01T00:00:00Z',
	},
	{
		id: 'acme-gold',
		project: 'acme',
		bucket: 'acme-gold-bucket',
		root_uri: 's3://acme-gold-bucket',
		status: 'active',
		serving: 'gold',
	},
];

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, { 'GET /v1/warehouses': WAREHOUSES });
});

test('a warehouse row opens the full record with the serving marker + project jump link', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/warehouses');
	// the gold serving record: click its PROJECT cell (id is a link, actions is a button)
	await page
		.locator('tbody tr', { hasText: 'acme-gold-bucket' })
		.getByText('acme', { exact: true })
		.click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('acme-gold');
	await expect(drawer).toContainText('s3://acme-gold-bucket');
	// the serving-class marker — a gold warehouse is stated as such, a work one is not
	await expect(drawer.getByText('serving · gold')).toBeVisible();
	// The project jump now LEAVES this zone: a project is the top of the hierarchy, so its page is
	// home's `/projects/<p>` (2026-08-03 ruling). Both halves are asserted, because either alone is
	// a bug that looks fine — a base-relative href would 404, and an absolute one without
	// `data-sveltekit-reload` soft-navigates into a route this zone's router does not own.
	const jump = drawer.getByRole('link', { name: 'Open project' });
	await expect(jump).toHaveAttribute('href', '/projects/acme');
	await expect(jump).toHaveAttribute('data-sveltekit-reload', '');
});

test('a work warehouse row states the work class honestly (no serving marker)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/warehouses');
	await page
		.locator('tbody tr', { hasText: 'acme-bucket' })
		.first()
		.getByText('acme', { exact: true })
		.click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('acme-wh');
	await expect(drawer).toContainText('work warehouse');
	await expect(drawer.getByText('serving · gold')).toHaveCount(0);
	await expect(drawer).toContainText('2026-07-01T00:00:00Z');
});
