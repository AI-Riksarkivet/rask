import { test, expect, type Route } from '@playwright/test';

// Hermetic #81 coverage: the SvelteFlow authorization graph is lazy-mounted from the table detail page.
// Stub the detail aggregate + access/graph; assert the graph renders the focus object + subject nodes, and
// that an inline grant posts through the BFF.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DETAIL = {
	describe: {
		version: 1,
		location: 's3://lance-catalog/db1$t',
		schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
	},
	stats: { num_rows: 1, total_bytes: 10, num_indices: 0 },
	versions: { versions: [{ version: 1, timestamp_millis: 1_700_000_000_000, manifest_size: 512 }] },
	tags: { tags: {} },
	branches: { branches: {} },
	indexes: { indexes: [] },
	policy: null,
};

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

let grantPost: { user: string; relation: string } | null;

test.beforeEach(async ({ page }) => {
	grantPost = null;
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path.endsWith('/detail')) return json(route, DETAIL);
		if (path.endsWith('/access/graph')) return json(route, GRAPH);
		if (path.endsWith('/access/grant')) {
			grantPost = req.postDataJSON() as { user: string; relation: string };
			return json(route, {
				object: 'table:db1$t',
				user: 'user:carol',
				relation: 'reader',
				granted: true,
			});
		}
		if (path.endsWith('/access/list')) return json(route, { object: 'table:db1$t', grants: [] });
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('lazy-shows the authorization graph with the focus object + subject nodes', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Show authorization graph' }).click();
	const graph = page.locator('.ag');
	await expect(graph.getByRole('heading', { name: 'Authorization graph' })).toBeVisible();
	// SvelteFlow renders the nodes as DOM — the focus object + the owner subject appear
	await expect(graph).toContainText('db1$t');
	await expect(graph).toContainText('alice');
	await expect(graph).toContainText('db1'); // the namespace container node
});

test('inline grant on the graph posts through the BFF', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
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
	await expect.poll(() => grantPost).toEqual({ user: 'carol', relation: 'reader' });
});
