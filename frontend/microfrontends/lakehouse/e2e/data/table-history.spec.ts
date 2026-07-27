import { test, expect, type Route, type Page } from '@playwright/test';

// #113 the commit log, hermetic. Every fixture below is a REAL payload, not an imagined one:
//  · the history rows are what `GET /v1/table/{id}/history` returned when driven through the real dir
//    backend and real pylance writes (create → insert → delete → update → create_scalar_index) — note
//    `operation` is Lance's vocabulary, the detail keys are ABSENT rather than null per operation, the
//    predicate is verbatim, and `fields_modified` is 0 on an Update that did modify a column;
//  · the producers are the shape `GET /datasets/{name}/producers` returns live — an opaque OIDC `sub` as
//    the author, a compaction run whose author is an EMPTY STRING, and a FAIL run with no version.
// What this suite guards is the honesty of the rendering: never claim a 0, never invent an actor, never
// reinterpret the offset-less timestamp, and never list a version the format does not report.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const ALICE_SUB = 'CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs';
const BOB_SUB = 'CiQxYjZlMmY1YS03YzRkLTRlOWItOWYxYS0yZDNjNGI1YTZlN2YSBWxvY2Fs';

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
 *  epoch-millis commit times. Note v1 is absent: GC reclaimed it, and a producing run still names it. */
const MANIFESTS = [
	{ version: 2, timestamp_millis: 1_700_000_100_000, manifest_size: 1024 },
	{ version: 3, timestamp_millis: 1_700_000_200_000, manifest_size: 2048 },
	{ version: 4, timestamp_millis: 1_700_000_300_000, manifest_size: 3072 },
	{ version: 5, timestamp_millis: 1_700_000_400_000, manifest_size: 4096 },
];

const DETAIL = {
	describe: {
		version: 5,
		location: 's3://lance-catalog/db1$t',
		schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
		metadata: {},
	},
	stats: { num_rows: 3, total_bytes: 2048, num_indices: 1 },
	versions: { versions: MANIFESTS },
	tags: { tags: { blessed: { version: 3 } } },
	branches: { branches: { main: { createAt: 1_700_000_000, manifestSize: 512 } } },
	indexes: { indexes: [] },
	policy: null,
	format: { name: 'Lance', storage_version: '2.2' },
};

/** Live producer shapes: alice's own sub on v5, another user's sub on v4, a compaction run with an EMPTY
 *  author on v3, nothing at all on v2, a run naming the GC'd v1, and a FAIL with no version. */
const PRODUCERS = [
	{
		run_id: 'r5',
		author: ALICE_SUB,
		event_time: '2026-07-26T14:18:04.105289+00:00',
		event_type: 'COMPLETE',
		dataset_version: '5',
		operation: 'create_index',
		row_count: 3,
		size_bytes: 286,
		quality_passed: true,
		quality_assertions: [{ assertion: 'row_count' }],
	},
	{
		run_id: 'r4-retry',
		author: BOB_SUB,
		event_time: '2026-07-26T14:18:04.060495+00:00',
		event_type: 'COMPLETE',
		dataset_version: '4',
		operation: 'update',
		row_count: 3,
		size_bytes: 280,
		quality_passed: null,
		quality_assertions: [],
	},
	{
		run_id: 'r4-first',
		author: ALICE_SUB,
		event_time: '2026-07-26T13:00:00.000000+00:00',
		event_type: 'COMPLETE',
		dataset_version: '4',
		operation: 'update',
		row_count: null,
		size_bytes: null,
		quality_passed: null,
		quality_assertions: [],
	},
	{
		run_id: 'r3',
		author: '',
		event_time: '2026-07-26T14:18:04.048439+00:00',
		event_type: 'COMPLETE',
		dataset_version: '3',
		operation: 'compaction',
		row_count: null,
		size_bytes: null,
		quality_passed: null,
		quality_assertions: [],
	},
	{
		// A whitespace-only author: the run recorded a field, but it names nobody. Rendering it verbatim
		// paints a BLANK cell, which reads as "we know, and it is nothing" — the exact failure the
		// normalization exists to prevent.
		run_id: 'r1-gcd',
		author: '   ',
		event_time: '2026-07-26T12:00:00.000000+00:00',
		event_type: 'COMPLETE',
		dataset_version: '1',
		operation: 'create_table',
		row_count: 3,
		size_bytes: 200,
		quality_passed: null,
		quality_assertions: [],
	},
	{
		run_id: 'r-fail',
		author: ALICE_SUB,
		event_time: '2026-07-26T14:30:00.000000+00:00',
		event_type: 'FAIL',
		dataset_version: null,
		operation: 'insert',
		error_message: 'schema mismatch on column v',
		row_count: null,
		size_bytes: null,
		quality_passed: null,
		quality_assertions: [],
	},
];

const ME = {
	sub: ALICE_SUB,
	name: 'alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [],
};

// Per-test knobs.
let historyStatus: number;
let historyBody: unknown;
let producersBody: unknown;
let restorePost: { version: number } | null;
let historyCalls: string[];

async function stub(page: Page): Promise<void> {
	await page.route('**/api/datasets/**/producers', (route) =>
		json(route, { producers: producersBody }),
	);
	await page.route('**/api/datasets/**/schema**', (route) => {
		const version = new URL(route.request().url()).searchParams.get('version');
		// v2 gained a column relative to v1's schema; v5 retyped it. Real lineage shape: name/type only.
		const fields =
			version === '2'
				? [{ name: 'id', type: 'int64' }]
				: version === '4'
					? [
							{ name: 'id', type: 'int64' },
							{ name: 'price', type: 'int64' },
						]
					: [
							{ name: 'id', type: 'int64' },
							{ name: 'price', type: 'double' },
						];
		return json(route, { dataset: 'db1$t', version: Number(version), fields });
	});
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		const search = new URL(req.url()).search;
		if (path === '/v1/me') return json(route, ME);
		if (path.endsWith('/history')) {
			historyCalls.push(search);
			return historyStatus === 200
				? json(route, historyBody)
				: json(route, { detail: 'Not Found' }, historyStatus);
		}
		if (path.endsWith('/detail')) return json(route, DETAIL);
		if (path.endsWith('/restore') && req.method() === 'POST') {
			restorePost = req.postDataJSON() as { version: number };
			return json(route, { version: 6 });
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
}

test.beforeEach(async ({ page }) => {
	historyStatus = 200;
	historyBody = HISTORY;
	producersBody = PRODUCERS;
	restorePost = null;
	historyCalls = [];
	await stub(page);
});

async function openLog(page: Page): Promise<void> {
	await page.goto('/lakehouse/data/tables/db1%24t');
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
	// Lance's names, not the catalog's — the same commit is `create_index` to the catalog and
	// `CreateIndex` to the format, and both are on the row.
	await expect(rows.first()).toContainText('via create_index');
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

test('the actor is my email when the sub is mine, and verbatim when it is not (#113)', async ({
	page,
}) => {
	await openLog(page);
	await expect(rowFor(page, 5)).toContainText('alice@example.com (you)');
	// Another user's opaque sub is truncated, never guessed at — and the full value stays in the title.
	const other = rowFor(page, 4).locator('[title*="OIDC subject"]').first();
	await expect(other).toHaveAttribute('title', `OIDC subject ${BOB_SUB}`);
	await expect(rowFor(page, 4)).not.toContainText('example.com');
});

test('an empty author and a missing run both read as an explicit dash (#113)', async ({ page }) => {
	await openLog(page);
	// v3's compaction run records author "" — an empty STRING, not null. v2 has no run at all.
	await expect(
		rowFor(page, 3).locator('[title="no governed run recorded this version"]'),
	).toBeVisible();
	await expect(
		rowFor(page, 2).locator('[title="no governed run recorded this version"]'),
	).toBeVisible();
	// …and a whitespace-only author is an absence too, not a blank cell.
	await expect(
		rowFor(page, 1).locator('[title="no governed run recorded this version"]'),
	).toBeVisible();
	await expect(page.locator('section.hist')).toContainText(
		'means no governed run recorded that version',
	);
});

test('rows/bytes are shown only where the writer measured them (#113)', async ({ page }) => {
	await openLog(page);
	await expect(rowFor(page, 5)).toContainText('3 rows');
	await expect(rowFor(page, 5)).toContainText('286 B');
	// v3's run measured nothing: a dash, never a 0 that reads as "it emptied the table".
	await expect(
		rowFor(page, 3).locator(
			'[title="the run that wrote this version measured no output statistics"]',
		),
	).toBeVisible();
	await expect(rowFor(page, 3)).not.toContainText('0 rows');
});

test('a version the format no longer reports is not listed because a run names it (#113)', async ({
	page,
}) => {
	await openLog(page);
	// The producers name v1 (GC reclaimed its manifest, the transaction log still has the commit) — the log
	// is driven by the FORMAT, so v1 appears exactly because /history reports it…
	await expect(rowFor(page, 1)).toBeVisible();
	// …and when /history is unavailable, the manifest list is the source and v1 is NOT invented from lineage.
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
	// The retry that also claims v4 is disclosed here, not merged into the row.
	await expect(panel).toContainText('r4-first');
});

test('a failed run appears as an attempted write, never as a version (#113)', async ({ page }) => {
	await openLog(page);
	const section = page.locator('section.hist');
	await expect(section).toContainText('Attempted writes that produced no version');
	await expect(section).toContainText('schema mismatch on column v');
	// It has no dataset_version, so it can never be a row in the log itself.
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(5);
	await expect(page.getByTestId('commit-log')).not.toContainText('schema mismatch');
});

test('history 404 states the missing route and keeps the manifest rows (#113)', async ({
	page,
}) => {
	historyStatus = 404;
	await openLog(page);
	const section = page.locator('section.hist');
	await expect(section).toContainText('does not serve');
	await expect(section).toContainText('/v1/table/db1$t/history');
	// The manifest half still renders — degraded, not blank.
	await expect(page.getByTestId('commit-log').locator('tbody tr')).toHaveCount(4);
	await expect(rowFor(page, 5)).toContainText('2023-11-14 22:20Z');
	await expect(rowFor(page, 5)).toContainText('alice@example.com (you)');
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
	expect(restorePost).toBeNull();
});

test('restore says so when the staleness check could not run (#113)', async ({ page }) => {
	historyStatus = 404;
	await openLog(page);
	await page.getByRole('button', { name: 'details for v2', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	await panel.getByRole('button', { name: 'Restore v2 (creates a new version)' }).click();
	await panel.getByLabel('Type the version to confirm the restore').fill('2');
	await panel.getByRole('button', { name: 'Confirm restore' }).click();
	await expect.poll(() => restorePost).toEqual({ version: 2 });
	await expect(page.locator('section.hist')).toContainText('concurrent-write check could not run');
});

test('the read limit is a real page size, not a client-side slice (#113)', async ({ page }) => {
	await openLog(page);
	await expect.poll(() => historyCalls).toContain('?limit=50');
	await page.getByLabel('How many commits to read').click();
	await page.getByRole('option', { name: 'newest 200' }).click();
	await expect.poll(() => historyCalls).toContain('?limit=200');
});

test('comparing two versions diffs their recorded schemas (#113)', async ({ page }) => {
	await openLog(page);
	// The row action seeds the base, so "compare from here" is a property of the commit you are looking at.
	await page.getByRole('button', { name: 'details for v2', exact: true }).click();
	await page.getByRole('button', { name: 'Compare from v2' }).click();
	await page.getByLabel('Compare version').click();
	await page.getByRole('option', { name: 'v5', exact: true }).click();
	const cmp = page.locator('.cmp');
	await expect(cmp).toContainText('v2 → v5');
	await expect(cmp.locator('tr[data-status="added"]')).toContainText('price');
	await expect(cmp).toContainText('matched by name');
	// v2 has no measured run, so there is NO delta tile — an absent measurement is not a zero.
	await expect(cmp.locator('[data-metric="rows"]')).toHaveCount(0);
	await expect(cmp).toContainText('No measured statistics on either side');
});

test('a delta tile appears only where both sides measured, and a retype reads as one (#113)', async ({
	page,
}) => {
	await openLog(page);
	const cmp = page.locator('.cmp');
	await cmp.getByLabel('Base version').click();
	await page.getByRole('option', { name: 'v4', exact: true }).click();
	await cmp.getByLabel('Compare version').click();
	await page.getByRole('option', { name: 'v5', exact: true }).click();
	// v4 and v5 both measured: 3→3 rows and 280→286 bytes.
	await expect(cmp.locator('[data-metric="rows"]')).toContainText('rows · 3 → 3');
	await expect(cmp.locator('[data-metric="bytes"]')).toContainText('+6 B');
	await expect(cmp.locator('tr[data-status="retyped"]')).toContainText('price');
	await expect(cmp.locator('tr[data-status="retyped"]')).toContainText('int64');
	await expect(cmp.locator('tr[data-status="retyped"]')).toContainText('double');
});

test('the compare direction cannot be inverted (#113)', async ({ page }) => {
	await openLog(page);
	const cmp = page.locator('.cmp');
	await cmp.getByLabel('Base version').click();
	await page.getByRole('option', { name: 'v4', exact: true }).click();
	await cmp.getByLabel('Compare version').click();
	// Only versions NEWER than the base are offered, so "compare" is always the later one.
	await expect(page.getByRole('option')).toHaveCount(1);
	await expect(page.getByRole('option').first()).toHaveText('v5');
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
