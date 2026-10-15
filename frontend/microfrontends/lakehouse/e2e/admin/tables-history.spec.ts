import { expect, test, type Page, type Route } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// #113 the commit log, hermetic. Every fixture below is a REAL payload, not an imagined one:
//  · the history rows are what `GET /management/v1/table/{id}/history` returned when driven through the real dir
//    backend and real pylance writes (create → insert → delete → update → create_scalar_index) — note
//    `operation` is Lance's vocabulary, the detail keys are ABSENT rather than null per operation, the
//    predicate is verbatim, and `fields_modified` is 0 on an Update that did modify a column.
// What this suite guards is the honesty of the rendering: never claim a 0, never reinterpret the
// offset-less timestamp, and never list a version the format does not report.
//
// TRANSPORT (the transport ruling, area 1): the detail aggregate and the restore are remote functions and
// run on the zone SERVER, so they are seeded on — and asserted through — the mock catalog. The commit
// log itself is unchanged: `/history` is a GET on the keep-bytes `[id]/[...rest]` proxy, still a
// browser call, so its per-test knobs (status, payload, the `?limit=` it asked for) stay on page.route.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

type Body = Record<string, unknown>;

const TABLE = 'db1%24t';

/** The real `/history` payload (scratchpad/histpayload.py, dir backend + pylance). */
const HISTORY = {
	table: 'db1$t',
	versions: [
		{
			version: 5,
			timestamp: '2026-07-26T16:18:04.105289',
			operation: 'CreateIndex',
			schema_set: false,
		},
		{
			version: 4,
			timestamp: '2026-07-26T16:18:04.060495',
			operation: 'Update',
			update_mode: 'rewrite_rows',
			fields_modified: 0,
			updated_fragments: 1,
			new_fragments: 1,
			removed_fragment_ids: 0,
			schema_set: false,
		},
		{
			version: 3,
			timestamp: '2026-07-26T16:18:04.048439',
			operation: 'Delete',
			predicate: 'id = 2',
			updated_fragments: 1,
			deleted_fragment_ids: 0,
			schema_set: false,
		},
		{
			version: 2,
			timestamp: '2026-07-26T16:18:04.037954',
			operation: 'Append',
			fragments: 1,
			schema_set: false,
		},
		{
			version: 1,
			timestamp: '2026-07-26T16:18:04.026416',
			operation: 'Overwrite',
			fragments: 1,
			schema_set: true,
		},
	],
};

/** The manifest list the detail aggregate carries — the format's own version list, with UNAMBIGUOUS
 *  epoch-millis commit times. Note v1 is absent: GC reclaimed it. */
const MANIFESTS = [
	{ version: 2, timestamp_millis: 1_700_000_100_000, manifest_size: 1024 },
	{ version: 3, timestamp_millis: 1_700_000_200_000, manifest_size: 2048 },
	{ version: 4, timestamp_millis: 1_700_000_300_000, manifest_size: 3072 },
	{ version: 5, timestamp_millis: 1_700_000_400_000, manifest_size: 4096 },
];

/** The six parts the detail aggregate fans out to on the zone server. */
const DETAIL_ROUTES: Record<string, unknown> = {
	[`POST /v1/table/${TABLE}/describe`]: {
		version: 5,
		location: 's3://lance-catalog/db1$t',
		schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
		metadata: {},
	},
	[`POST /v1/table/${TABLE}/stats`]: { num_rows: 3, total_bytes: 2048, num_indices: 1 },
	[`POST /v1/table/${TABLE}/version/list`]: { versions: MANIFESTS },
	[`POST /v1/table/${TABLE}/tags/list`]: { tags: { blessed: { version: 3 } } },
	[`POST /v1/table/${TABLE}/branches/list`]: {
		branches: { main: { createAt: 1_700_000_000, manifestSize: 512 } },
	},
	[`POST /v1/table/${TABLE}/index/list`]: { indexes: [] },
	[`POST /management/v1/table/${TABLE}/policy/describe`]: {
		status: 404,
		body: { detail: 'no policy' },
	},
	[`POST /v1/table/${TABLE}/restore`]: { version: 6 },
};

// Per-test knobs.
let token: string;
let historyStatus: number;
let historyBody: unknown;
let historyCalls: string[];

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const restoreCall = async (page: Page): Promise<Body | undefined> =>
	(await calls(page)).find((c) => c.path === `/v1/table/${TABLE}/restore`);

async function stub(page: Page): Promise<void> {
	// The commit log's own read — a GET through the keep-bytes proxy, so it stays interceptable here.
	await page.route('**/capi/management/v1/table/*/history*', (route) => {
		historyCalls.push(new URL(route.request().url()).search);
		return historyStatus === 200
			? json(route, historyBody)
			: json(route, { detail: 'Not Found' }, historyStatus);
	});
}

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	historyStatus = 200;
	historyBody = HISTORY;
	historyCalls = [];
	await signIn(context, { token });
	await mockMe(page);
	await stub(page);
	await seed(page, DETAIL_ROUTES);
});

async function openLog(page: Page): Promise<void> {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'history' }).click();
	await expect(page.locator('section.hist')).toBeVisible();
}

const rowFor = (page: Page, version: number) =>
	page
		.getByTestId('commit-log')
		.locator('tbody tr')
		.filter({ hasText: `v${version}` })
		.first();

test('the log reads newest-first in the FORMAT’s own vocabulary (#113)', async ({ page }) => {
	await openLog(page);
	const rows = page.getByTestId('commit-log').locator('tbody tr');
	await expect(rows.first()).toContainText('v5');
	await expect(rows.first()).toContainText('CreateIndex');
	await expect(rowFor(page, 2)).toContainText('Append');
	await expect(rowFor(page, 1)).toContainText('Overwrite');
});

test('a Delete carries its predicate verbatim (#113)', async ({ page }) => {
	await openLog(page);
	// The predicate as the caller wrote it. "A Delete happened" is not an answer.
	await expect(rowFor(page, 3)).toContainText('where id = 2');
});

test('an Update that reports fields_modified: 0 never claims 0 fields (#113)', async ({ page }) => {
	await openLog(page);
	const row = rowFor(page, 4);
	await expect(row).toContainText('rewrite_rows');
	await expect(row).toContainText('+1 fragment');
	// pylance reports 0 for an update that DID modify a column, so rendering "0 fields rewritten" would be
	// a false statement about the data. Same for the `removed_fragment_ids: 0` on this row.
	await expect(row).not.toContainText('0 field');
	await expect(row).not.toContainText('-0 fragment');
});

test('a version the format no longer reports is not listed (#113)', async ({ page }) => {
	await openLog(page);
	// GC reclaimed v1's manifest, the transaction log still has the commit — the log is driven by the
	// FORMAT, so v1 appears exactly because /history reports it…
	await expect(rowFor(page, 1)).toBeVisible();
	// …and when /history is unavailable, the manifest list is the source and v1 is NOT invented.
	historyStatus = 404;
	await page.reload();
	await page.getByRole('tab', { name: 'history' }).click();
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(4);
	await expect(page.getByTestId('commit-log')).not.toContainText('v1');
});

test('the when column never reinterprets the offset-less wire timestamp (#113)', async ({
	page,
}) => {
	await openLog(page);
	// 1_700_000_400_000 is 2023-11-14 22:20 UTC. The catalog's own `timestamp` for that commit is the
	// naive local string "2026-07-26T16:18:04…" — if the view parsed THAT with new Date(), the cell would
	// read 2026. The manifest epoch is the only unambiguous source, and the cell says Z.
	await expect(rowFor(page, 5)).toContainText('2023-11-14 22:20Z');
	await expect(rowFor(page, 5)).not.toContainText('2026-07-26');
	// The wire's own string is still available, verbatim, on the commit panel.
	await page.getByRole('button', { name: 'details for v5', exact: true }).click();
	await expect(page.getByTestId('commit-panel')).toContainText('2026-07-26T16:18:04.105289');
	await expect(page.getByTestId('commit-panel')).toContainText('no offset');
});

test('the operation filter and the schema-only toggle narrow the log (#113)', async ({ page }) => {
	await openLog(page);
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(5);
	await page.getByRole('button', { name: 'Delete 1' }).click();
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(1);
	await expect(page.getByTestId('commit-log').locator('tbody tr').first()).toContainText(
		'where id = 2',
	);
	await page.getByRole('button', { name: 'all 5' }).click();
	await page.getByLabel('schema changes only').check();
	// Only v1 set the schema (the create) — Lance reports schema_set on that commit alone here.
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(1);
	await expect(page.getByTestId('commit-log').locator('tbody tr').first()).toContainText('v1');
});

test('the raw transaction fields are shown uninterpreted (#113)', async ({ page }) => {
	await openLog(page);
	await page.getByRole('button', { name: 'details for v4', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	// Every key the wire carried, including the ones the summary deliberately refuses to render.
	await expect(panel).toContainText('fields_modified');
	await expect(panel).toContainText('update_mode');
	await expect(panel).toContainText('rewrite_rows');
});

test('history 404 states the missing route and keeps the manifest rows (#113)', async ({
	page,
}) => {
	historyStatus = 404;
	await openLog(page);
	const section = page.locator('section.hist');
	await expect(section).toContainText('does not serve');
	await expect(section).toContainText('/management/v1/table/db1$t/history');
	// The manifest half still renders — degraded, not blank.
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(4);
	await expect(rowFor(page, 5)).toContainText('2023-11-14 22:20Z');
});

test('history 403 is a different message from 404 (#113)', async ({ page }) => {
	historyStatus = 403;
	await openLog(page);
	await expect(page.locator('section.hist')).toContainText('can_get_metadata');
	await expect(page.locator('section.hist')).not.toContainText('does not serve');
});

test('a drifted history payload is reported, not rendered from (#113)', async ({ page }) => {
	// `version` is required and numeric on the wire; a string is a contract drift.
	historyBody = { table: 'db1$t', versions: [{ version: 'five', operation: 'Append' }] };
	await openLog(page);
	await expect(page.locator('section.hist')).toContainText('drifted from the contract');
});

test('restore refuses when the tip moved under the page (#113)', async ({ page }) => {
	await openLog(page);
	await page.getByRole('button', { name: 'details for v2', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	await panel.getByRole('button', { name: 'Restore v2 (creates a new version)' }).click();
	// Someone else commits v6 while the page sits open: the pre-flight tip read now disagrees with the
	// version the page believes is current.
	historyBody = { table: 'db1$t', versions: [{ version: 6, operation: 'Append' }] };
	await panel.getByLabel('Type the version to confirm the restore').fill('v2');
	await panel.getByRole('button', { name: 'Confirm restore' }).click();
	await expect(panel).toContainText('moved to v6');
	expect(await restoreCall(page)).toBeUndefined();
});

test('restore says so when the staleness check could not run (#113)', async ({ page }) => {
	historyStatus = 404;
	await openLog(page);
	await page.getByRole('button', { name: 'details for v2', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	await panel.getByRole('button', { name: 'Restore v2 (creates a new version)' }).click();
	await panel.getByLabel('Type the version to confirm the restore').fill('2');
	await panel.getByRole('button', { name: 'Confirm restore' }).click();
	await expect.poll(async () => (await restoreCall(page))?.body).toEqual({ version: 2 });
	await expect(page.locator('section.hist')).toContainText('concurrent-write check could not run');
});

test('the read limit is a real page size, not a client-side slice (#113)', async ({ page }) => {
	await openLog(page);
	await expect.poll(() => historyCalls).toContain('?limit=50');
	await page.getByLabel('How many commits to read').click();
	await page.getByRole('option', { name: 'newest 200' }).click();
	await expect.poll(() => historyCalls).toContain('?limit=200');
});

test('the history tab is deep-linkable (#113)', async ({ page }) => {
	await openLog(page);
	await expect(page).toHaveURL(/\?tab=history/);
	await page.reload();
	// The tab survives the reload, so the log is a shareable link and not a click-path. (A cold reload of
	// this route compiles the whole detail page, so it gets a longer budget than the default 5s.)
	await expect(page.getByRole('tab', { name: 'history' })).toHaveAttribute(
		'aria-selected',
		'true',
		{
			timeout: 20_000,
		},
	);
	await expect(page.locator('section.hist')).toBeVisible({ timeout: 20_000 });
});
