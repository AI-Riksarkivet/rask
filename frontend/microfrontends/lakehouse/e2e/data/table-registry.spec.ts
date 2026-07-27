import { test, expect, type Route } from '@playwright/test';

// Hermetic /tables registry coverage (#85 declare): declare_table is the browser-shaped create — a
// JSON body (no Arrow payload) POSTed to /v1/table/{ns}${name}/declare, after which the registry
// re-lists and shows the reserved row. The /capi BFF is stubbed (same pattern as namespaces.spec.ts);
// a successful declare mutates the mocked registry so the reload returns the post-declare world.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

let tables: string[] = [];
let declarePost: { path: string; body: unknown } | null = null;

test.beforeEach(async ({ page }) => {
	tables = ['db1$existing'];
	declarePost = null;
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/table') {
			return json(route, { tables });
		}
		const decl = /^\/v1\/table\/([^/]+)\/declare$/.exec(path);
		if (decl && req.method() === 'POST') {
			declarePost = { path, body: req.postDataJSON() as unknown };
			tables = [...tables, decodeURIComponent(decl[1])];
			return json(route, { location: 's3://lance-catalog/db1$fresh' });
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('declare-table posts a JSON body (no Arrow) and the registry re-lists the new row (#85)', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/tables');
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('fresh');
	// location left empty — the catalog picks; the wire body must then be EMPTY, not {location: ""}
	await page.getByRole('button', { name: 'Declare', exact: true }).click();
	// The exact wire contract, %24-encoded id included — poll, don't race the interception.
	await expect.poll(() => declarePost).toEqual({ path: '/v1/table/db1%24fresh/declare', body: {} });
	await expect(page.locator('.banner.ok')).toContainText(
		'declared db1$fresh @ s3://lance-catalog/db1$fresh',
	);
	// the success re-load renders the post-declare registry: the reserved row is listed
	await expect(page.getByRole('link', { name: 'db1$fresh' })).toBeVisible();
});

test('declare with a location carries it on the wire (#85)', async ({ page }) => {
	await page.goto('/lakehouse/data/tables');
	// Hydration guard (same as the first declare test): clicking the toggle before Svelte attaches the
	// handler is a silent no-op and the form never opens.
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('pinned');
	await page.getByLabel('Location').fill('s3://elsewhere/db1$pinned');
	await page.getByRole('button', { name: 'Declare', exact: true }).click();
	await expect
		.poll(() => declarePost)
		.toEqual({
			path: '/v1/table/db1%24pinned/declare',
			body: { location: 's3://elsewhere/db1$pinned' },
		});
});

test('a 403 declare renders the create-denied state and adds no row (#85)', async ({ page }) => {
	// A later page.route wins over the beforeEach glob — deny like the catalog's FGA gate
	// (can_create_table on the parent namespace) does for a non-writer.
	await page.route('**/capi/v1/table/*/declare', (route) =>
		json(route, { detail: 'forbidden' }, 403),
	);
	await page.goto('/lakehouse/data/tables');
	// Hydration guard — see the location test above.
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('fresh');
	await page.getByRole('button', { name: 'Declare', exact: true }).click();
	await expect(page.locator('.banner.fail')).toContainText(
		'Denied: declaring in db1 needs create access (can_create_table).',
	);
	await expect(page.getByRole('link', { name: 'db1$fresh' })).toHaveCount(0);
});
