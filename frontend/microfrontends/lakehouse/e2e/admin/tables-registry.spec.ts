import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic /tables registry coverage (#85 declare): declare_table is the browser-shaped create — a
// JSON body (no Arrow payload) POSTed to /v1/table/{ns}${name}/declare, after which the registry
// re-lists and shows the reserved row.
//
// The transport is remote functions now (the transport ruling, area 1): both the list and the declare
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

// --- the other half of the form: create WITH data (the dead end's exit) ---
//
// A bare declare reserves an id and writes nothing, and the append door (`/insert`) opens a table's
// dataset to coerce the batch — so it 404s for exactly the table that was just declared. The rows
// field routes the same submit through the catalog's create door instead, which lands the first data
// version into a declared-only table's already-reserved location. Without it, "Declare table" was a
// one-way street.

test('rows in the form send the CREATE door an Arrow body, not the declare door', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables');
	// Hydration guard — see the location test above.
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await seed(page, {
		'GET /v1/table': { tables: ['db1$existing', 'db1$fresh'] },
		'POST /v1/table/db1%24fresh/create': { version: 1 },
	});
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('fresh');
	await page.getByLabel('Initial rows').fill('[{ "id": 1, "name": "a" }]');
	// The submit RENAMES itself once rows are present — one form, two doors, no hidden mode switch.
	await page.getByRole('button', { name: 'Create with rows', exact: true }).click();

	// `body: null` is the assertion, not a gap: the mock records a JSON body or nothing, and this
	// payload is Arrow IPC — the create door is the zone's keep-bytes route, so a JSON body here would
	// mean the transport ruling had been broken.
	await expect
		.poll(() => callTo(page, '/v1/table/db1%24fresh/create'))
		.toEqual({ method: 'POST', path: '/v1/table/db1%24fresh/create', body: null });
	await expect(await callTo(page, '/v1/table/db1%24fresh/declare')).toBeUndefined();
	await expect(page.locator('.banner.ok')).toContainText('Created db1$fresh with 1 row.');
	// The re-list, and it is load-bearing: this write is a keep-bytes BFF route, so nothing on the
	// zone server invalidates the `fetchTables` query cache the way the declare COMMAND does. Without
	// the explicit refresh in the component this assertion fails until the live cursor happens to
	// tick — which is exactly how it first showed up, as a flake rather than a failure.
	await expect(page.getByRole('link', { name: 'db1$fresh' })).toBeVisible();
});

test('a bare declare says the reserved id holds no data yet', async ({ page }) => {
	// The dead end was not only missing a door — it was silent about being a dead end, and "declared
	// db1$fresh" read as "the table is ready" right before its detail page 404'd.
	await page.goto('/lakehouse/catalog/tables');
	await expect(page.getByRole('link', { name: 'db1$existing' })).toBeVisible();
	await seed(page, { 'POST /v1/table/db1%24fresh/declare': { location: 's3://x' } });
	await page.getByRole('button', { name: 'Declare table' }).click();
	await page.getByLabel('Namespace').fill('db1');
	await page.getByLabel('Table name').fill('fresh');
	await page.getByRole('button', { name: 'Declare', exact: true }).click();

	await expect(page.locator('.banner.ok')).toContainText('no data yet');
	await expect(page.locator('.banner.ok')).toContainText('appending rows to it is refused');
});
