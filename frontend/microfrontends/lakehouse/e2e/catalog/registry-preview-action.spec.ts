import { test, expect, type Route } from '@playwright/test';
import { Int32, Table, Utf8, tableToIPC, vectorFromArray } from 'apache-arrow';

// The R18 table-previewer entry point: a registry ROW's drawer carries a Preview action that
// deep-links onto the detail pane's preview tab (?tab=preview), which drives the EXISTING /capi
// query machinery — first-N rows parsed from real Arrow bytes onto the shared data-table. The tab
// itself is covered by table-preview.spec.ts; this asserts the path from a row to it.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DETAIL = {
	describe: { version: 1, schema: { fields: [{ name: 'id', type: 'int32', nullable: false }] } },
	stats: null,
	versions: null,
	tags: null,
	branches: null,
	indexes: null,
	policy: null,
};

function arrowFixture(): Buffer {
	const table = new Table({
		id: vectorFromArray([1, 2], new Int32()),
		name: vectorFromArray(['alpha', 'beta'], new Utf8()),
	});
	return Buffer.from(tableToIPC(table, 'file'));
}

test.beforeEach(async ({ page }) => {
	await page.route('**/api/datasets/**/producers', (route) => json(route, { producers: [] }));
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/table') return json(route, { tables: ['raw$events'] });
		if (path.endsWith('/detail')) return json(route, DETAIL);
		if (/^\/v1\/table\/[^/]+\/query$/.test(path) && req.method() === 'POST') {
			return route.fulfill({
				status: 200,
				contentType: 'application/vnd.apache.arrow.file',
				body: arrowFixture(),
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
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
