import { test, expect, type Route } from '@playwright/test';

// Hermetic /namespaces coverage (#64 + the #85 drop lifecycle): the page derives namespaces from the
// catalog table list (there is no root-list endpoint), grouped by the `<namespace>$<table>` prefix.
// Mock the /capi calls it makes — the list, and the POST /v1/namespace/{id}/drop write (a successful
// drop mutates the mocked registry so the next poll returns the post-drop world).

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

let lastCapiPath = '';
let tables: string[] = [];
let dropPost: { path: string; body: unknown } | null = null;

test.beforeEach(async ({ page }) => {
	lastCapiPath = '';
	tables = ['bronze$events', 'gold$catalog', 'gold$metrics'];
	dropPost = null;
	await page.route('**/capi/**', (route) => {
		const pathname = new URL(route.request().url()).pathname;
		// The layout's /capi/v1/me identity fetch shares the glob — track only the registry call here.
		if (!pathname.endsWith('/v1/me')) lastCapiPath = pathname;
		const path = pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/me') return json(route, { detail: 'anon' }, 401);
		if (path === '/v1/table') {
			return json(route, { tables });
		}
		const drop = /^\/v1\/namespace\/([^/]+)\/drop$/.exec(path);
		if (drop) {
			const ns = decodeURIComponent(drop[1]);
			dropPost = { path, body: route.request().postDataJSON() as unknown };
			tables = tables.filter((t) => t !== ns && !t.startsWith(`${ns}$`));
			return json(route, {});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('the client fetches the BFF under the zone base path, not a bare /capi', async ({ page }) => {
	// Regression lock for the base-path bug: the zone is served under /data, so its BFF proxy lives at
	// /data/capi/* — a bare /capi never reaches this zone through the Ingress (proven: bare → 404). The
	// mocked glob (**/capi/**) matches both, so THIS asserts the real request carries the base.
	await page.goto('/lakehouse/data/namespaces');
	// Poll — the fetch fires from an $effect after mount, so wait for the intercept rather than racing it.
	await expect.poll(() => lastCapiPath).toBe('/lakehouse/capi/v1/table');
});

test('lists one sortable row per namespace with table counts + tier badges', async ({ page }) => {
	await page.goto('/lakehouse/data/namespaces');
	await expect(page.getByRole('heading', { name: 'Namespaces' })).toBeVisible();
	// One DataTable row per namespace (goal cond 4), derived from the registry ids.
	const bronze = page.locator('tr', { has: page.locator('a.ns-name', { hasText: 'bronze' }) });
	await expect(bronze).toContainText('1');
	await expect(bronze.locator('.stage')).toHaveText('bronze'); // the derived medallion tier badge
	const gold = page.locator('tr', { has: page.locator('a.ns-name', { hasText: /^gold$/ }) });
	await expect(gold).toContainText('2');
	// the name links into the namespace detail view
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toHaveAttribute(
		'href',
		'/lakehouse/data/namespaces/bronze',
	);
	// the text search narrows the rows
	await page.getByPlaceholder('Search namespaces…').fill('bronze');
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toHaveCount(0);
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toBeVisible();
});

test('the New-namespace affordance points at the governed warehouse-bind flow', async ({
	page,
}) => {
	// Deliberate scope: no bare create surface here — creation goes through POST
	// /v1/warehouses/{id}/namespaces on the warehouses page (bucket-per-warehouse tenancy).
	await page.goto('/lakehouse/data/namespaces');
	await expect(page.getByRole('link', { name: 'New namespace' })).toHaveAttribute(
		'href',
		'/lakehouse/data/warehouses',
	);
});

test('drop confirms via the AlertDialog, posts the cascade behavior, and the row disappears', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces');
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toBeVisible();
	await page.getByRole('button', { name: 'Drop namespace bronze', exact: true }).click();
	// The confirm is the portalled @rask/ui AlertDialog — drive it by role, not the trigger row.
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace bronze');
	await dialog.getByRole('checkbox').check(); // Cascade — bronze holds a table, Restrict would refuse
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	// Poll (don't race the route interception — same rule as the base-path test above).
	await expect
		.poll(() => dropPost)
		.toEqual({ path: '/v1/namespace/bronze/drop', body: { behavior: 'Cascade' } });
	// The dialog must CLOSE after the drop — a still-open dialog keeps the destructive action armed for a
	// second confirm-free fire (audit: major).
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	// The success re-load renders the post-drop registry: bronze gone, gold intact.
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toHaveCount(0);
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toBeVisible();
	await expect(page.locator('.banner.ok')).toContainText('namespace bronze dropped (cascade)');
});

test('an unticked confirm posts Restrict — the default must never silently cascade', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace gold');
	// Leave the cascade checkbox UNTICKED: the wire contract must carry Restrict (refuse-on-non-empty) —
	// this pins the default so a mapping inversion can't silently cascade-destroy a namespace.
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	await expect
		.poll(() => dropPost)
		.toEqual({ path: '/v1/namespace/gold/drop', body: { behavior: 'Restrict' } });
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('cancelling the confirm never posts the drop', async ({ page }) => {
	await page.goto('/lakehouse/data/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	expect(dropPost).toBeNull();
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toBeVisible();
});

test('a 403 drop surfaces the forbidden state and keeps the namespace listed', async ({ page }) => {
	// A later page.route wins over the beforeEach glob — deny the drop like the catalog's FGA gate
	// (owner-tier can_delete) does for a non-owner.
	await page.route('**/capi/v1/namespace/**', (route) => json(route, { detail: 'forbidden' }, 403));
	await page.goto('/lakehouse/data/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Drop', exact: true }).click();
	await expect(page.locator('.banner.fail')).toContainText(
		'Denied: dropping gold needs the owner rung (can_delete).',
	);
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toBeVisible();
});

test('the shared sidebar marks the Namespaces leaf active', async ({ page }) => {
	await page.goto('/lakehouse/data/namespaces');
	// The AppShell sidebar (shared @rask/ui) reflects the current route via data-active.
	await expect(
		page.locator('[data-active="true"]').filter({ hasText: 'Namespaces' }),
	).toBeVisible();
});
