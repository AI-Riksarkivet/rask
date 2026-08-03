import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic /namespaces coverage (#64 + the #85 drop lifecycle): the page derives namespaces from the
// catalog table list (there is no root-list endpoint), grouped by the `<namespace>$<table>` prefix.
// Both the list and the drop are remote functions now (the transport ruling, area 1) — they run on the
// zone SERVER with the signed-in session's bearer — so the registry is seeded on the mock catalog and
// the drop asserted through its per-bearer ledger.
//
// One assertion of the /capi-era spec is deliberately retired rather than translated: it pinned the
// registry read to `/lakehouse/capi/v1/table` (the zone-base-path regression lock for the BFF proxy).
// That call no longer exists — the read is a remote function on the zone's own server, and the
// base-path class of bug it guarded is gone with it. What replaces it is stronger: the list is served
// per BEARER, so the render below is proof the zone server reached the catalog as THIS user.

type Body = Record<string, unknown>;

const ALL_TABLES = { tables: ['bronze$events', 'gold$catalog', 'gold$metrics'] };
/** The post-drop world a successful cascade drop of `bronze` leaves behind. */
const AFTER_DROP = { tables: ['gold$catalog', 'gold$metrics'] };

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
	await seed(page, {
		'GET /v1/table': ALL_TABLES,
		'POST /v1/namespace/bronze/drop': {},
		'POST /v1/namespace/gold/drop': {},
	});
});

test('lists one sortable row per namespace with table counts + tier badges', async ({ page }) => {
	await page.goto('/lakehouse/catalog/namespaces');
	await expect(page.getByRole('heading', { name: 'Namespaces' })).toBeVisible();
	// One DataTable row per namespace (goal cond 4), derived from the registry ids the zone server
	// read from the catalog with this test's own bearer.
	const bronze = page.locator('tr', { has: page.locator('a.ns-name', { hasText: 'bronze' }) });
	await expect(bronze).toContainText('1');
	await expect(bronze.locator('.stage')).toHaveText('bronze'); // the derived medallion tier badge
	const gold = page.locator('tr', { has: page.locator('a.ns-name', { hasText: /^gold$/ }) });
	await expect(gold).toContainText('2');
	// the name links into the namespace detail view
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/namespaces/bronze',
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
	await page.goto('/lakehouse/catalog/namespaces');
	await expect(page.getByRole('link', { name: 'New namespace' })).toHaveAttribute(
		'href',
		'/lakehouse/catalog/warehouses',
	);
});

test('drop confirms via the AlertDialog, posts the cascade behavior, and the row disappears', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/namespaces');
	await expect(page.locator('a.ns-name', { hasText: 'bronze' })).toBeVisible();
	// The post-drop registry, staged BEFORE the write: the drop refreshes the list in the same flight,
	// so what the page re-renders from is the catalog's answer, never a local splice.
	await seed(page, { 'GET /v1/table': AFTER_DROP });
	await page.getByRole('button', { name: 'Drop namespace bronze', exact: true }).click();
	// The confirm is the portalled @rask/ui AlertDialog — drive it by role, not the trigger row.
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace bronze');
	await dialog.getByRole('checkbox').check(); // Cascade — bronze holds a table, Restrict would refuse
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	// Poll (don't race the ledger) — the cascade choice is a BODY field, not a query param.
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/bronze/drop'))
		.toMatchObject({ method: 'POST', body: { behavior: 'Cascade' } });
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
	await page.goto('/lakehouse/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace gold');
	// Leave the cascade checkbox UNTICKED: the wire contract must carry Restrict (refuse-on-non-empty) —
	// this pins the default so a mapping inversion can't silently cascade-destroy a namespace.
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/gold/drop'))
		.toMatchObject({ method: 'POST', body: { behavior: 'Restrict' } });
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('cancelling the confirm never posts the drop', async ({ page }) => {
	await page.goto('/lakehouse/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	expect(await callTo(page, '/v1/namespace/gold/drop')).toBeUndefined();
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toBeVisible();
});

test('a 403 drop surfaces the forbidden state and keeps the namespace listed', async ({ page }) => {
	// Deny the drop like the catalog's FGA gate (owner-tier can_delete) does for a non-owner.
	await seed(page, {
		'POST /v1/namespace/gold/drop': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Drop', exact: true }).click();
	await expect(page.locator('.banner.fail')).toContainText(
		'Denied: dropping gold needs the owner rung (can_delete).',
	);
	await expect(page.locator('a.ns-name', { hasText: /^gold$/ })).toBeVisible();
});

test('the shared sidebar marks the Namespaces leaf active', async ({ page }) => {
	await page.goto('/lakehouse/catalog/namespaces');
	// The AppShell sidebar (shared @rask/ui) reflects the current route via data-active.
	await expect(
		page.locator('[data-active="true"]').filter({ hasText: 'Namespaces' }),
	).toBeVisible();
});
