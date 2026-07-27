import { test, expect, type Route } from '@playwright/test';

// The hierarchy drill-down (goal cond 3): project → warehouse → namespace → table, driven with an
// alice-shaped /v1/me (estate admin, one membership). Hermetic: every /capi call is stubbed at the
// browser boundary. Tier badges are asserted where the namespace names encode a medallion stage.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const ME_ALICE = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

const PROJECTS = [
	{
		project: 'acme',
		warehouses: [{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' }],
		admins: ['alice'],
	},
];

const WAREHOUSE = {
	id: 'acme-wh',
	project: 'acme',
	bucket: 'acme-bucket',
	root_uri: 's3://acme-bucket',
	status: 'active',
};

const TABLES = ['acme-silver$features', 'acme-gold$catalog', 'bronze$events'];

test.beforeEach(async ({ page }) => {
	await page.route('**/capi/**', (route) => {
		const path = new URL(route.request().url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/me') return json(route, ME_ALICE);
		if (path === '/v1/projects') return json(route, PROJECTS);
		if (path === '/v1/projects/acme') return json(route, PROJECTS[0]);
		if (path === '/v1/warehouses/acme-wh') return json(route, WAREHOUSE);
		if (path === '/v1/table') return json(route, { tables: TABLES });
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('drills project → warehouse → namespace with tier badges along the way', async ({ page }) => {
	// The projects rung: the estate list renders acme with its role + warehouse count.
	await page.goto('/lakehouse/data/projects');
	const acmeRow = page.locator('a.row', { hasText: 'acme' });
	await expect(acmeRow).toContainText('admin');
	await expect(acmeRow).toContainText('1 warehouse');
	await acmeRow.click();

	// The project rung: warehouses table links into the warehouse page.
	await expect(page).toHaveURL(/\/data\/projects\/acme$/);
	await expect(page.getByRole('heading', { name: 'acme' })).toBeVisible();
	await expect(page.getByText('acme-bucket')).toBeVisible();
	await page.getByRole('link', { name: 'acme-wh' }).click();

	// The warehouse rung: registry facts + namespaces derived by the <project>-<stage> convention,
	// each carrying its tier badge; the foreign project's bare `bronze` zone must NOT appear.
	await expect(page).toHaveURL(/\/data\/warehouses\/acme-wh$/);
	await expect(page.getByText('s3://acme-bucket')).toBeVisible();
	const silverRow = page.locator('a.row', { hasText: 'acme-silver' });
	await expect(silverRow).toBeVisible();
	await expect(silverRow.locator('.stage')).toHaveText('silver');
	await expect(page.locator('a.row', { hasText: 'acme-gold' }).locator('.stage')).toHaveText(
		'gold',
	);
	await expect(page.locator('a.row', { hasText: 'bronze$events' })).toHaveCount(0);
	await silverRow.click();

	// The namespace rung (existing page): its member table listed.
	await expect(page).toHaveURL(/\/data\/namespaces\/acme-silver$/);
	await expect(page.getByRole('link', { name: 'acme-silver$features' })).toBeVisible();
});

test('the table detail header carries the derived tier badge', async ({ page }) => {
	await page.route('**/capi/v1/table/acme-gold%24catalog/detail', (route) =>
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
	await page.goto('/lakehouse/data/tables/acme-gold%24catalog');
	await expect(page.locator('header .stage')).toHaveText('gold');
	// The tab bar defaults to overview; the Access tab hosts the FGA view.
	await expect(page.getByRole('tab', { name: 'overview' })).toHaveAttribute(
		'aria-selected',
		'true',
	);
	await page.getByRole('tab', { name: 'access' }).click();
	await expect(page.getByRole('button', { name: 'Access review' })).toBeVisible();
	// Overview content is not rendered on the access tab.
	await expect(page.getByRole('heading', { name: 'Schema' })).toHaveCount(0);
});

test('a member without the estate privilege still gets their membership gallery', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) =>
		json(route, {
			...ME_ALICE,
			sub: 'user:bob',
			name: 'Bob',
			estate_admin: false,
			projects: [{ project: 'acme', role: 'member' }],
		}),
	);
	await page.route('**/capi/v1/projects', (route) => json(route, { detail: 'forbidden' }, 403));
	await page.goto('/lakehouse/data/projects');
	await expect(page.getByText('your memberships')).toBeVisible();
	await expect(page.locator('a.row', { hasText: 'acme' })).toContainText('member');
});
