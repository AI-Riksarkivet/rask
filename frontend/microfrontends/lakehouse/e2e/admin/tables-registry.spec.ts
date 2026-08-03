import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic /tables registry coverage (#85 declare): declare_table is the browser-shaped create — a
// JSON body (no Arrow payload) POSTed to /v1/table/{ns}${name}/declare, after which the registry
// re-lists and shows the reserved row.
//
// The transport is remote functions now (open_transport.md, area 1): both the list and the declare
// run on the zone SERVER, which `page.route` cannot see — so the wire is seeded on, and asserted
// through, the mock catalog's per-bearer ledger. Same assertions as the /capi-era spec, including the
// %24-encoded id and the EMPTY body when no location is given.

type Body = Record<string, unknown>;

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const callTo = async (page: Page, path: string): Promise<Body | undefined> =>
	(await calls(page)).find((c) => c.path === path);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, { 'GET /v1/table': { tables: ['db1$existing'] } });
});

test('declare-table posts a JSON body (no Arrow) and the registry re-lists the new row (#85)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables');
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	// The post-declare world: the catalog answers the create, and the registry it re-lists now carries
	// the reserved id. Seeded before the click because the page re-lists in the same flight.
	await seed(page, {
		'GET /v1/table': { tables: ['db1$existing', 'db1$fresh'] },
		'POST /v1/table/db1%24fresh/declare': { location: 's3://lance-catalog/db1$fresh' },
	});
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('fresh');
	// location left empty — the catalog picks; the wire body must then be EMPTY, not {location: ""}
	await page.getByRole('button', { name: 'Declare', exact: true }).click();
	// The exact wire contract, %24-encoded id included — poll, don't race the request.
	await expect
		.poll(() => callTo(page, '/v1/table/db1%24fresh/declare'))
		.toEqual({ method: 'POST', path: '/v1/table/db1%24fresh/declare', body: {} });
	await expect(page.locator('.banner.ok')).toContainText(
		'declared db1$fresh @ s3://lance-catalog/db1$fresh',
	);
	// the success re-load renders the post-declare registry: the reserved row is listed
	await expect(page.getByRole('link', { name: 'db1$fresh' })).toBeVisible();
});

test('declare with a location carries it on the wire (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables');
	// Hydration guard (same as the first declare test): clicking the toggle before Svelte attaches the
	// handler is a silent no-op and the form never opens.
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await seed(page, { 'POST /v1/table/db1%24pinned/declare': { location: 's3://elsewhere' } });
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('pinned');
	await page.getByLabel('Location').fill('s3://elsewhere/db1$pinned');
	await page.getByRole('button', { name: 'Declare', exact: true }).click();
	await expect
		.poll(() => callTo(page, '/v1/table/db1%24pinned/declare'))
		.toEqual({
			method: 'POST',
			path: '/v1/table/db1%24pinned/declare',
			body: { location: 's3://elsewhere/db1$pinned' },
		});
});

test('a 403 declare renders the create-denied state and adds no row (#85)', async ({ page }) => {
	// Deny like the catalog's FGA gate (can_create_table on the parent namespace) does for a non-writer.
	await seed(page, {
		'POST /v1/table/db1%24fresh/declare': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/tables');
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
