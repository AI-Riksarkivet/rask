import { expect, test, type Page, type Route } from '@playwright/test';
import { Int32, Table, Utf8, tableToIPC, vectorFromArray } from 'apache-arrow';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// The R18 table-previewer entry point: a registry ROW's drawer carries a Preview action that
// deep-links onto the detail pane's preview tab (?tab=preview), which drives the EXISTING /capi
// query machinery — first-N rows parsed from real Arrow bytes onto the shared data-table. The tab
// itself is covered by tables-preview.spec.ts; this asserts the path from a row to it.
//
// The registry list and the detail aggregate are remote functions now (open_transport.md, area 1),
// so they are seeded on the mock catalog; the Arrow query keeps its bytes route and its intercept.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const TABLE = 'raw%24events';
const ABSENT = { status: 404, body: { detail: 'none' } };

const DETAIL_ROUTES: Record<string, unknown> = {
	[`POST /v1/table/${TABLE}/describe`]: {
		version: 1,
		schema: { fields: [{ name: 'id', type: 'int32', nullable: false }] },
	},
	[`POST /v1/table/${TABLE}/stats`]: ABSENT,
	[`POST /v1/table/${TABLE}/version/list`]: ABSENT,
	[`POST /v1/table/${TABLE}/tags/list`]: ABSENT,
	[`POST /v1/table/${TABLE}/branches/list`]: ABSENT,
	[`POST /v1/table/${TABLE}/index/list`]: ABSENT,
	[`POST /v1/table/${TABLE}/policy/describe`]: ABSENT,
};

function arrowFixture(): Buffer {
	const table = new Table({
		id: vectorFromArray([1, 2], new Int32()),
		name: vectorFromArray(['alpha', 'beta'], new Utf8()),
	});
	return Buffer.from(tableToIPC(table, 'file'));
}

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await page.route('**/api/datasets/**/producers', (route) => json(route, { producers: [] }));
	await page.route('**/capi/v1/table/*/query', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.file',
			body: arrowFixture(),
		}),
	);
	await seed(page, { ...DETAIL_ROUTES, 'GET /v1/table': { tables: ['raw$events'] } });
});

test('the drawer Preview action lands on the detail preview tab with rows', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables');
	// open the row drawer via the stage cell (the id/namespace cells are links of their own)
	await page.locator('tbody tr', { hasText: 'raw$events' }).locator('.stage').click();
	const drawer = page.getByRole('dialog');
	await expect(drawer.getByRole('link', { name: 'Preview' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/tables/raw%24events?tab=preview',
	);
	await drawer.getByRole('link', { name: 'Preview' }).click();
	// the deep link selects the preview tab, and the existing query machinery renders the rows
	await expect(page.getByRole('tab', { name: 'preview' })).toHaveAttribute('aria-selected', 'true');
	const section = page.locator('section', { hasText: 'Preview' });
	await expect(section.getByRole('cell', { name: 'alpha' })).toBeVisible();
	await expect(section.getByRole('cell', { name: 'beta' })).toBeVisible();
});
