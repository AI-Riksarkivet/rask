import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// The residual table/namespace rungs of e2e/catalog/row-drawers.spec.ts + hierarchy.spec.ts, moved
// here when their registry/detail reads became remote functions (seeded on the mock catalog per
// bearer — page.route cannot see a server-side read). Every behavioural assertion is kept; the two
// URL regexes that still said `/data/tables/…` are corrected to `/catalog/tables/…`, the rename the
// zone shipped earlier (the links themselves were fixed in 4f63249; these expectations lagged).

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
	await page.route('**/api/datasets/**/producers', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: '{"producers":[]}' }),
	);
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer: token,
			routes: {
				'GET /v1/table': { tables: ['db1$t', 'raw$events', 'acme-gold$catalog'] },
				[describeKey('raw$events')]: DESCRIBE,
				[describeKey('db1$t')]: DESCRIBE,
				[describeKey('acme-gold$catalog')]: DESCRIBE,
			},
		},
	});
});

test('a table row opens the record drawer with stage, namespace and the two jump links', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables');
	// click the row's STAGE badge (the id/namespace cells are links with their own navigation)
	await page.locator('tbody tr', { hasText: 'raw$events' }).locator('.stage').click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('raw$events');
	await expect(drawer).toContainText('The registry record for this table.');
	await expect(drawer).toContainText('medallion stage');
	// jump links: the detail page, and the detail page's Access tab (?tab= deep link)
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/tables/raw%24events',
	);
	await expect(drawer.getByRole('link', { name: 'Access tab' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/tables/raw%24events?tab=access',
	);
});

test('the access jump link lands on the detail page with the Access tab selected', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables');
	await page.locator('tbody tr', { hasText: 'raw$events' }).locator('.stage').click();
	await page.getByRole('dialog').getByRole('link', { name: 'Access tab' }).click();
	await expect(page).toHaveURL(/\/catalog\/tables\/raw%24events\?tab=access$/);
	await expect(page.getByRole('tab', { name: 'access' })).toHaveAttribute('aria-selected', 'true');
});

test('an in-cell table link navigates WITHOUT opening the drawer (stopPropagation)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables');
	await page.getByRole('link', { name: 'db1$t', exact: true }).click();
	await expect(page).toHaveURL(/\/catalog\/tables\/db1%24t$/);
	await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('a namespace row opens the record drawer with the member count', async ({ page }) => {
	await page.goto('/lakehouse/catalog/namespaces');
	// the db1 namespace groups one table — click its COUNT cell (the name cell is a link)
	await page.locator('tbody tr', { hasText: 'db1' }).getByText('1', { exact: true }).click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('db1');
	await expect(drawer).toContainText('tables');
	await expect(drawer.getByRole('link', { name: 'Open detail' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/namespaces/db1',
	);
});

test('the table detail header carries the derived tier badge', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/acme-gold%24catalog');
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
