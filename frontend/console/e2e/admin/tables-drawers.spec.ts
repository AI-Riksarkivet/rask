import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// The table/namespace row drawers, over registry and detail reads seeded on the mock catalog per bearer.

const ME_ALICE = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

// The detail aggregate fans out to the catalog's OWN endpoints (describe gates the page; every other
// part is optional and an unseeded read 404s into an honest `null` part) — so only describe is seeded.
const DESCRIBE = { version: 1, schema: { fields: [{ name: 'id', type: 'int64' }] } };
const describeKey = (table: string) =>
	`POST /v1/table/${encodeURIComponent(table)}/describe?load_detailed_metadata=true&with_table_uri=true`;

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page, ME_ALICE);
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer: token,
			routes: {
				'GET /v1/table': { tables: ['db1$t', 'bronze$events', 'acme-gold$catalog'] },
				[describeKey('bronze$events')]: DESCRIBE,
				[describeKey('db1$t')]: DESCRIBE,
				[describeKey('acme-gold$catalog')]: DESCRIBE,
			},
		},
	});
});

test('a table row opens the record drawer with stage, namespace and the two jump links', async ({
	page,
}) => {
	await page.goto('/catalog/tables');
	// click the row's STAGE badge (the id/namespace cells are links with their own navigation)
	await page.locator('tbody tr', { hasText: 'bronze$events' }).locator('.stage').click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('bronze$events');
	await expect(drawer).toContainText('The registry record for this table.');
	await expect(drawer).toContainText('medallion stage');
	// jump links: the detail page, and the detail page's Access tab (?tab= deep link)
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/catalog/tables/bronze%24events',
	);
	await expect(drawer.getByRole('link', { name: 'Access tab' })).toHaveAttribute(
		'href',
		'/catalog/tables/bronze%24events?tab=access',
	);
});

test('the access jump link lands on the detail page with the Access tab selected', async ({
	page,
}) => {
	await page.goto('/catalog/tables');
	await page.locator('tbody tr', { hasText: 'bronze$events' }).locator('.stage').click();
	await page.getByRole('dialog').getByRole('link', { name: 'Access tab' }).click();
	await expect(page).toHaveURL(/\/catalog\/tables\/bronze%24events\?tab=access$/);
	await expect(page.getByRole('tab', { name: 'access' })).toHaveAttribute('aria-selected', 'true');
});

test('an in-cell table link navigates in the app, with no page load and no drawer', async ({
	page,
}) => {
	await page.goto('/catalog/tables');
	// The list renders once the first visit's sign-in round trip is over, so loads count from here.
	const link = page.getByRole('link', { name: 'db1$t', exact: true });
	await expect(link).toBeVisible();
	// A page load drops the in-memory sign-in, so the router must be the one to follow the link. The mock
	// issuer signs a reloaded page straight back in, which is why the URL alone cannot tell the two apart.
	const loads: string[] = [];
	page.on('request', (request) => {
		if (request.resourceType() === 'document') loads.push(request.url());
	});
	await link.click();
	await expect(page).toHaveURL(/\/catalog\/tables\/db1%24t$/);
	await expect(page.getByRole('dialog')).toHaveCount(0);
	expect(loads).toEqual([]);
});

test('a namespace row opens the record drawer with the member count', async ({ page }) => {
	await page.goto('/catalog/namespaces');
	// the db1 namespace groups one table — click its COUNT cell (the name cell is a link)
	await page.locator('tbody tr', { hasText: 'db1' }).getByText('1', { exact: true }).click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('db1');
	await expect(drawer).toContainText('tables');
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/catalog/namespaces/db1',
	);
});

test('the table detail header carries the derived tier badge', async ({ page }) => {
	await page.goto('/catalog/tables/acme-gold%24catalog');
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
