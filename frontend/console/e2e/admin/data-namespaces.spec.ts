import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// Hermetic /namespaces coverage (#64 + the #85 drop lifecycle + the create): the page lists each
// warehouse's namespaces through the spec's ListNamespaces (the root's children are the warehouses,
// a warehouse's its namespaces) and counts their tables from the catalog table list. Every id opens
// with its warehouse (`acme-wh$bronze$events`). The list, the create and the drop are data functions
// in `*.api.ts`, run in the browser with the signed-in session's bearer, so the catalog is seeded on
// the mock and the writes asserted through its per-bearer ledger.

type Body = Record<string, unknown>;

const ALL_TABLES = {
	tables: ['acme-wh$bronze$events', 'acme-wh$gold$catalog', 'acme-wh$gold$metrics'],
};
/** The post-drop world a successful cascade drop of `acme-wh$bronze` leaves behind. */
const AFTER_DROP = { tables: ['acme-wh$gold$catalog', 'acme-wh$gold$metrics'] };
/** The warehouse's own listing: its namespace NAMES, `silver` among them though it holds no table. */
const LISTED = { namespaces: ['bronze', 'gold', 'silver'] };
const AFTER_DROP_LISTED = { namespaces: ['gold', 'silver'] };

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
		'GET /v1/namespace/%24/list': { namespaces: ['acme-wh'] },
		'GET /v1/namespace/acme-wh/list': LISTED,
		'POST /v1/namespace/acme-wh%24bronze/drop': {},
		'POST /v1/namespace/acme-wh%24gold/drop': {},
	});
});

/** A namespace's row link, by its whole id. */
const nsLink = (page: Page, id: string) =>
	page.locator('a.ns-name', { hasText: new RegExp(`^${id.replaceAll('$', '\\$')}$`) });

test('lists one sortable row per namespace with table counts + tier badges', async ({ page }) => {
	await page.goto('/catalog/namespaces');
	await expect(page.getByRole('heading', { name: 'Namespaces' })).toBeVisible();
	// One DataTable row per namespace (goal cond 4), derived from the registry ids the browser
	// read from the catalog with this test's own bearer.
	const bronze = page.locator('tr', { has: nsLink(page, 'acme-wh$bronze') });
	await expect(bronze).toContainText('1');
	await expect(bronze.locator('.stage')).toHaveText('bronze'); // the derived medallion tier badge
	await expect(bronze).toContainText('acme-wh'); // its warehouse, read off its id
	const gold = page.locator('tr', { has: nsLink(page, 'acme-wh$gold') });
	await expect(gold).toContainText('2');
	// A namespace that holds no table is listed by its warehouse, so it has a row all the same (#66).
	const silver = page.locator('tr', { has: nsLink(page, 'acme-wh$silver') });
	await expect(silver).toContainText('0');
	// the name links into the namespace detail view
	await expect(nsLink(page, 'acme-wh$bronze')).toHaveAttribute(
		'href',
		'/catalog/namespaces/acme-wh%24bronze',
	);
	// the text search narrows the rows
	await page.getByPlaceholder('Search namespaces…').fill('bronze');
	await expect(nsLink(page, 'acme-wh$gold')).toHaveCount(0);
	await expect(nsLink(page, 'acme-wh$bronze')).toBeVisible();
});

test('New namespace creates one in the chosen warehouse through the spec door, and lists it', async ({
	page,
}) => {
	// The create is the spec's CreateNamespace on `<warehouse>$<name>`, mode Create, so a name already
	// there is refused rather than kept; the warehouse's listing re-reads in the same flight.
	await seed(page, { 'POST /v1/namespace/acme-wh%24raw/create': { properties: {} } });
	await page.goto('/catalog/namespaces');
	await page.getByRole('button', { name: 'New namespace' }).click();
	const form = page.getByRole('form', { name: 'Create namespace' });
	// The form says why Create waits, and a name holding the delimiter is no name of its own.
	await expect(form.locator('#create-hint')).toHaveText('Creating needs a warehouse and a name.');
	await form.getByLabel('Warehouse').click();
	await page.getByRole('option', { name: 'acme-wh', exact: true }).click();
	await form.getByRole('textbox', { name: 'Name' }).fill('a$b');
	await expect(form.locator('#create-hint')).toContainText('A name holds no $');
	await form.getByRole('textbox', { name: 'Name' }).fill('raw');
	await seed(page, {
		'GET /v1/namespace/acme-wh/list': { namespaces: ['bronze', 'gold', 'raw', 'silver'] },
	});
	await form.getByRole('button', { name: 'Create' }).click();

	await expect
		.poll(async () => await callTo(page, '/v1/namespace/acme-wh%24raw/create'))
		.toMatchObject({ method: 'POST', body: { mode: 'Create' } });
	await expect(page.locator('.banner.ok')).toContainText('namespace acme-wh$raw created');
	await expect(nsLink(page, 'acme-wh$raw')).toBeVisible();
});

test('drop confirms via the AlertDialog, posts the cascade behavior, and the row disappears', async ({
	page,
}) => {
	await page.goto('/catalog/namespaces');
	await expect(nsLink(page, 'acme-wh$bronze')).toBeVisible();
	// The post-drop catalog, staged BEFORE the write: the drop refreshes the list in the same flight,
	// so what the page re-renders from is the catalog's answer, never a local splice.
	await seed(page, {
		'GET /v1/table': AFTER_DROP,
		'GET /v1/namespace/acme-wh/list': AFTER_DROP_LISTED,
	});
	await page.getByRole('button', { name: 'Drop namespace acme-wh$bronze', exact: true }).click();
	// The confirm is the portalled @rask/ui AlertDialog — drive it by role, not the trigger row.
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace acme-wh$bronze');
	await dialog.getByRole('checkbox').check(); // Cascade — bronze holds a table, Restrict would refuse
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	// Poll (don't race the ledger) — the cascade choice is a BODY field, not a query param.
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/acme-wh%24bronze/drop'))
		.toMatchObject({ method: 'POST', body: { behavior: 'Cascade' } });
	// The dialog must CLOSE after the drop — a still-open dialog keeps the destructive action armed for a
	// second confirm-free fire (audit: major).
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	// The success re-load renders the post-drop catalog: bronze gone, gold intact.
	await expect(nsLink(page, 'acme-wh$bronze')).toHaveCount(0);
	await expect(nsLink(page, 'acme-wh$gold')).toBeVisible();
	await expect(page.locator('.banner.ok')).toContainText(
		'namespace acme-wh$bronze dropped (cascade)',
	);
});

test('an unticked confirm posts Restrict — the default must never silently cascade', async ({
	page,
}) => {
	await page.goto('/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace acme-wh$gold', exact: true }).click();
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop namespace acme-wh$gold');
	// Leave the cascade checkbox UNTICKED: the wire contract must carry Restrict (refuse-on-non-empty) —
	// this pins the default so a mapping inversion can't silently cascade-destroy a namespace.
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	await expect
		.poll(async () => await callTo(page, '/v1/namespace/acme-wh%24gold/drop'))
		.toMatchObject({ method: 'POST', body: { behavior: 'Restrict' } });
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
});

test('cancelling the confirm never posts the drop', async ({ page }) => {
	await page.goto('/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace acme-wh$gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	expect(await callTo(page, '/v1/namespace/acme-wh%24gold/drop')).toBeUndefined();
	await expect(nsLink(page, 'acme-wh$gold')).toBeVisible();
});

test('a 403 drop surfaces the forbidden state and keeps the namespace listed', async ({ page }) => {
	// Deny the drop like the catalog's FGA gate (owner-tier can_delete) does for a non-owner.
	await seed(page, {
		'POST /v1/namespace/acme-wh%24gold/drop': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/catalog/namespaces');
	await page.getByRole('button', { name: 'Drop namespace acme-wh$gold', exact: true }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Drop', exact: true }).click();
	await expect(page.locator('.banner.fail')).toContainText(
		'Denied: dropping acme-wh$gold needs the owner rung (can_delete).',
	);
	await expect(nsLink(page, 'acme-wh$gold')).toBeVisible();
});

test('the shared sidebar marks the Namespaces leaf active', async ({ page }) => {
	await page.goto('/catalog/namespaces');
	// The AppShell sidebar (shared @rask/ui) reflects the current route via data-active.
	await expect(
		page.locator('[data-active="true"]').filter({ hasText: 'Namespaces' }),
	).toBeVisible();
});

test('a warehouse whose listing failed says so — it never reads as "no namespaces" (#86)', async ({
	page,
}) => {
	// THE point of #86: this page seeds its rows from each warehouse's listing so an empty namespace
	// is visible (#83), and a listing that could not answer must degrade LOUDLY, never into an empty
	// array — #66's exact shape, an invisible namespace with nothing said. A deactivated warehouse
	// answers its listing 403, and that is said by name too.
	await seed(page, {
		'GET /v1/namespace/acme-wh/list': { status: 403, body: { detail: 'warehouse deactivated' } },
	});
	await page.goto('/catalog/namespaces');
	await expect(page.getByTestId('namespaces-partial')).toContainText(
		'The namespaces of warehouse acme-wh could not be listed',
	);
	await expect(page.getByTestId('namespaces-partial')).toContainText('warehouse deactivated');
	// …and the table-derived rows still render, because the degrade is PARTIAL, not a blank page:
	// what is lost is only the empty namespaces, which is exactly what the banner says.
	await expect(nsLink(page, 'acme-wh$bronze')).toBeVisible();
	await expect(nsLink(page, 'acme-wh$silver')).toHaveCount(0);
});
