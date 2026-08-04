import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic #81 coverage: the SvelteFlow authorization graph is lazy-mounted from the table detail
// page. Its read and its inline grant are remote functions now (the transport ruling, area 1) — both run
// on the zone SERVER — so the graph is seeded on the mock catalog and the grant asserted through its
// per-bearer ledger. Same assertions as the /capi-era spec: the focus object + subject nodes render,
// and an inline grant reaches the catalog with the exact {user, relation} body.

type Body = Record<string, unknown>;

const TABLE = 'db1%24t'; // the id as it reaches the catalog: encodeURIComponent('db1$t')

const GRAPH = {
	object: 'table:db1$t',
	nodes: [
		{ id: 'table:db1$t', type: 'table', label: 'db1$t' },
		{ id: 'user:alice', type: 'user', label: 'alice' },
		{ id: 'namespace:db1', type: 'namespace', label: 'db1' },
	],
	edges: [
		{ source: 'user:alice', target: 'table:db1$t', relation: 'owner' },
		{ source: 'table:db1$t', target: 'namespace:db1', relation: 'parent' },
	],
};

/** The table-detail aggregate's upstream parts — the page is gated on `describe`. */
const DETAIL_ROUTES = {
	[`POST /v1/table/${TABLE}/describe`]: {
		version: 1,
		location: 's3://lance-catalog/db1$t',
		schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
	},
	[`POST /v1/table/${TABLE}/stats`]: { num_rows: 1, total_bytes: 10, num_indices: 0 },
	[`POST /v1/table/${TABLE}/version/list`]: {
		versions: [{ version: 1, timestamp_millis: 1_700_000_000_000, manifest_size: 512 }],
	},
	[`POST /v1/table/${TABLE}/tags/list`]: { tags: {} },
	[`POST /v1/table/${TABLE}/branches/list`]: { branches: {} },
	[`POST /v1/table/${TABLE}/index/list`]: { indexes: [] },
	[`POST /v1/table/${TABLE}/policy/describe`]: { status: 404, body: { detail: 'no policy' } },
};

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const callTo = async (page: Page, path: string): Promise<Body | undefined> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls.find((c) => c.path === path);
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, {
		...DETAIL_ROUTES,
		[`POST /v1/table/${TABLE}/access/graph`]: GRAPH,
		[`POST /v1/table/${TABLE}/access/list`]: { object: 'table:db1$t', grants: [] },
		[`POST /v1/table/${TABLE}/access/grant`]: {
			object: 'table:db1$t',
			user: 'user:carol',
			relation: 'reader',
			granted: true,
		},
	});
});

test('lazy-shows the authorization graph with the focus object + subject nodes', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Show authorization graph' }).click();
	const graph = page.locator('.ag');
	await expect(graph.getByRole('heading', { name: 'Authorization graph' })).toBeVisible();
	// SvelteFlow renders the nodes as DOM — the focus object + the owner subject appear
	await expect(graph).toContainText('db1$t');
	await expect(graph).toContainText('alice');
	await expect(graph).toContainText('db1'); // the namespace container node
});

test('inline grant on the graph reaches the catalog', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Show authorization graph' }).click();
	const graph = page.locator('.ag');
	await graph.getByPlaceholder('user (e.g. alice), or role:… / team:…#member').fill('carol');
	// force: the detail page's poll re-renders shift layout just enough that Playwright's stability
	// sampler starves under the 30s budget; the trigger is present+enabled (asserted) and the wire
	// assertion below is the real contract, so skipping the stability wait is safe here.
	await expect(graph.getByLabel('Rung')).toBeEnabled();
	await graph.getByLabel('Rung').click({ force: true });
	await page.getByRole('option', { name: 'reader', exact: true }).click();
	await graph.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect
		.poll(async () => (await callTo(page, `/v1/table/${TABLE}/access/grant`))?.body)
		.toEqual({ user: 'carol', relation: 'reader' });
});
