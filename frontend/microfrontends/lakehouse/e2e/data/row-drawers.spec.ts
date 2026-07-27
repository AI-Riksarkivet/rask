import { test, expect, type Route } from '@playwright/test';

// Hermetic row-drawer coverage (goal cond 8): clicking a registry row (tables / namespaces /
// warehouses) opens the bits-ui drawer with the full record + jump links; in-cell links and action
// buttons stopPropagation, so they keep their own behavior without also opening the drawer.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const TABLES = ['db1$t', 'raw$events'];

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

test.beforeEach(async ({ page }) => {
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/table') return json(route, { tables: TABLES });
		if (path === '/v1/warehouses' && req.method() === 'GET') return json(route, WAREHOUSES);
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('a table row opens the record drawer with stage, namespace and the two jump links', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/tables');
	// click the row's STAGE badge (the id/namespace cells are links with their own navigation)
	await page.locator('tbody tr', { hasText: 'raw$events' }).locator('.stage').click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('raw$events');
	await expect(drawer).toContainText('The registry record for this table.');
	await expect(drawer).toContainText('medallion stage');
	// jump links: the detail page, and the detail page's Access tab (?tab= deep link)
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/lakehouse/data/tables/raw%24events',
	);
	await expect(drawer.getByRole('link', { name: 'Access tab' })).toHaveAttribute(
		'href',
		'/lakehouse/data/tables/raw%24events?tab=access',
	);
});

test('the access jump link lands on the detail page with the Access tab selected', async ({
	page,
}) => {
	await page.route('**/api/datasets/**/producers', (route) => json(route, { producers: [] }));
	await page.route('**/capi/v1/table/raw%24events/detail', (route) =>
		json(route, {
			describe: { version: 1, schema: { fields: [{ name: 'id', type: 'int64' }] } },
			stats: null,
			versions: null,
			tags: null,
			branches: null,
			indexes: null,
			policy: null,
		}),
	);
	await page.goto('/lakehouse/data/tables');
	await page.locator('tbody tr', { hasText: 'raw$events' }).locator('.stage').click();
	await page.getByRole('dialog').getByRole('link', { name: 'Access tab' }).click();
	await expect(page).toHaveURL(/\/data\/tables\/raw%24events\?tab=access$/);
	await expect(page.getByRole('tab', { name: 'access' })).toHaveAttribute('aria-selected', 'true');
});

test('an in-cell table link navigates WITHOUT opening the drawer (stopPropagation)', async ({
	page,
}) => {
	await page.route('**/api/datasets/**/producers', (route) => json(route, { producers: [] }));
	await page.route('**/capi/v1/table/db1%24t/detail', (route) =>
		json(route, {
			describe: { version: 1, schema: { fields: [{ name: 'id', type: 'int64' }] } },
			stats: null,
			versions: null,
			tags: null,
			branches: null,
			indexes: null,
			policy: null,
		}),
	);
	await page.goto('/lakehouse/data/tables');
	await page.getByRole('link', { name: 'db1$t', exact: true }).click();
	await expect(page).toHaveURL(/\/data\/tables\/db1%24t$/);
	await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('a namespace row opens the record drawer with the member count', async ({ page }) => {
	await page.goto('/lakehouse/data/namespaces');
	// the db1 namespace groups one table — click its COUNT cell (the name cell is a link)
	await page.locator('tbody tr', { hasText: 'db1' }).getByText('1', { exact: true }).click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('db1');
	await expect(drawer).toContainText('tables');
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/lakehouse/data/namespaces/db1',
	);
});

test('a warehouse row opens the full record with the serving marker + project jump link', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/warehouses');
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
	await expect(drawer.getByRole('link', { name: 'Open project' })).toHaveAttribute(
		'href',
		'/lakehouse/data/projects/acme',
	);
});

test('a work warehouse row states the work class honestly (no serving marker)', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/warehouses');
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
