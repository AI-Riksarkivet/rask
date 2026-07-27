import { test, expect, type Route } from '@playwright/test';
import { Binary, Int32, Table, Utf8, tableToIPC, vectorFromArray } from 'apache-arrow';

// Hermetic PREVIEW-tab coverage (goal cond 5): the tab posts {"limit": N} to this zone's explicit
// /capi/v1/table/{id}/query BFF route and parses the Arrow-IPC FILE response with apache-arrow.
// The fixture bytes here are REAL Arrow (built with the same library), so the parse path — schema
// extraction, typed headers, BigInt/blob cells — is exercised, not mocked around.

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

// Three rows: an int column, a string column, and a binary (blob) column whose cells must render
// as the honest "<blob NNkB>" chip, never inline bytes.
function arrowFixture(): Buffer {
	const table = new Table({
		id: vectorFromArray([1, 2, 3], new Int32()),
		name: vectorFromArray(['alpha', 'beta', 'gamma'], new Utf8()),
		payload: vectorFromArray(
			[new Uint8Array(2048), new Uint8Array(512), new Uint8Array(64)],
			new Binary(),
		),
	});
	return Buffer.from(tableToIPC(table, 'file'));
}

function emptyFixture(): Buffer {
	const table = new Table({ id: vectorFromArray([], new Int32()) });
	return Buffer.from(tableToIPC(table, 'file'));
}

let queryBody: Record<string, unknown> | null;
let queryResponse: Buffer;

test.beforeEach(async ({ page }) => {
	queryBody = null;
	queryResponse = arrowFixture();
	await page.route('**/api/datasets/**/producers', (route) => json(route, { producers: [] }));
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path.endsWith('/detail')) return json(route, DETAIL);
		if (/^\/v1\/table\/[^/]+\/query$/.test(path) && req.method() === 'POST') {
			queryBody = req.postDataJSON() as Record<string, unknown>;
			return route.fulfill({
				status: 200,
				contentType: 'application/vnd.apache.arrow.file',
				body: queryResponse,
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('the preview tab posts {limit} and renders the typed grid from real Arrow bytes', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	// the BFF contract: the browser sends ONLY the row budget
	await expect.poll(() => queryBody).toEqual({ limit: 50 });
	const section = page.locator('section', { hasText: 'Preview' });
	// typed headers: column name + its Arrow type from the parsed schema
	await expect(section.getByRole('columnheader', { name: /id/ })).toContainText('Int32');
	await expect(section.getByRole('columnheader', { name: /name/ })).toContainText('Utf8');
	// plain cells render values; the string column arrives verbatim
	await expect(section.getByRole('cell', { name: 'alpha' })).toBeVisible();
	await expect(section.getByRole('cell', { name: 'gamma' })).toBeVisible();
});

test('blob cells render the honest size chip, never raw bytes (goal cond 5)', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	const section = page.locator('section', { hasText: 'Preview' });
	// 2048 B → 2kB, 512 B → 0.5kB — the chip states the size, the bytes stay opaque
	await expect(section.getByText('<blob 2kB>')).toBeVisible();
	await expect(section.getByText('<blob 0.5kB>')).toBeVisible();
});

test('a changed row limit reloads with the new budget', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	const section = page.locator('section', { hasText: 'Preview' });
	await expect.poll(() => queryBody).toEqual({ limit: 50 });
	await section.getByLabel('Preview row limit').fill('5');
	await section.getByRole('button', { name: 'Load' }).click();
	await expect.poll(() => queryBody).toEqual({ limit: 5 });
});

test('an empty table states the honest empty message', async ({ page }) => {
	queryResponse = emptyFixture();
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	await expect(
		page.getByText('No rows — the table is empty at its current version.'),
	).toBeVisible();
});

test('a 403 preview renders the reader-denied state (never an empty grid)', async ({ page }) => {
	await page.route('**/capi/v1/table/*/query', (route) =>
		json(route, { detail: 'forbidden' }, 403),
	);
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	await expect(
		page.getByText("You don't have read access to this table's data (can_read_data)."),
	).toBeVisible();
});

test('a non-Arrow body surfaces the parse-drift error instead of lying', async ({ page }) => {
	await page.route('**/capi/v1/table/*/query', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.file',
			body: Buffer.from('this is not arrow'),
		}),
	);
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'preview' }).click();
	await expect(page.getByText(/preview response is not readable Arrow IPC/)).toBeVisible();
});

test('?tab=preview deep-links straight onto the tab', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t?tab=preview');
	await expect(page.getByRole('tab', { name: 'preview' })).toHaveAttribute('aria-selected', 'true');
	await expect.poll(() => queryBody).toEqual({ limit: 50 });
});
