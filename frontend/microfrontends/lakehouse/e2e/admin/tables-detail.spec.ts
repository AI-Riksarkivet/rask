import { expect, test, type Page, type Route } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic /tables/<id> coverage (#64/#66/#65/#73/#74/#75/#76/#85). Guards the version-management
// surface the wrong-image deploy proved was unguarded: the manifest-per-commit version table, the
// branches row, the tag-a-version form, and the two-click restore control — plus the maintenance,
// schema-evolution, index, row-op and danger-zone writes.
//
// The transport is remote functions now (the transport ruling, area 1): the detail aggregate and every
// write run on the zone SERVER, which `page.route` cannot see. So the catalog wire is seeded on, and
// asserted through, the mock catalog's per-bearer ledger — and the assertions get SHARPER, because
// the upstream path is now the operation's own name (`add_columns`, `create_index`, `tags/create`)
// rather than this zone's `[op]`/`[action]` allowlist segment.
//
// Two things stay at the browser boundary and stay on `page.route`: the Arrow-IPC row insert (a
// keep-bytes route by verdict) and the lineage producers read behind the quality badge.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

type Body = Record<string, unknown>;

const TABLE = 'db1%24t'; // the id as it reaches the catalog: encodeURIComponent('db1$t')

const DESCRIBE = {
	version: 3,
	location: 's3://lance-catalog/db1$t',
	schema: {
		fields: [{ name: 'id', type: 'int64', nullable: false, metadata: { unit: 'count' } }],
	},
	metadata: { owner: 'data-eng' }, // #74 tail — table-level schema metadata (properties editor seed)
};

const MANIFESTS = [
	{ version: 1, timestamp_millis: 1_700_000_000_000, manifest_size: 512 },
	{ version: 2, timestamp_millis: 1_700_000_100_000, manifest_size: 1024 },
	{ version: 3, timestamp_millis: 1_700_000_200_000, manifest_size: 2048 },
];

/** The six POST-shaped catalog reads the detail aggregate fans out to, seeded part by part — the
 *  page is gated on `describe`, the rest are per-part optional. */
const DETAIL_ROUTES: Record<string, unknown> = {
	[`POST /v1/table/${TABLE}/describe`]: DESCRIBE,
	[`POST /v1/table/${TABLE}/stats`]: { num_rows: 100, total_bytes: 2048, num_indices: 1 },
	[`POST /v1/table/${TABLE}/version/list`]: { versions: MANIFESTS },
	[`POST /v1/table/${TABLE}/tags/list`]: { tags: { blessed: { version: 2 } } },
	[`POST /v1/table/${TABLE}/branches/list`]: {
		branches: {
			main: { createAt: 1_700_000_000, manifestSize: 512 },
			dev: { createAt: 1_700_000_100, manifestSize: 1024 },
		},
	},
	[`POST /v1/table/${TABLE}/index/list`]: {
		indexes: [{ index_name: 'id_idx', columns: ['id'], index_type: 'BTREE' }],
	},
	[`POST /v1/table/${TABLE}/policy/describe`]: {
		retention_days: 7,
		retain_versions: 5,
		compact_enabled: true,
		target_rows_per_fragment: 1048576,
	},
};

/** #113 the commit log the History tab reads (a GET on the keep-bytes `[id]/[...rest]` proxy). The
 *  tip agrees with `describe.version`, so the restore control's staleness pre-flight passes. */
const HISTORY = {
	table: 'db1$t',
	versions: [
		{ version: 3, timestamp: '2026-07-26T16:18:04.105289', operation: 'Append' },
		{ version: 2, timestamp: '2026-07-26T16:18:04.060495', operation: 'Append' },
		{ version: 1, timestamp: '2026-07-26T16:18:04.026416', operation: 'Overwrite' },
	],
};

/** Every write the page can make, answered exactly as the catalog would. */
const WRITE_ROUTES: Record<string, unknown> = {
	[`POST /v1/table/${TABLE}/tags/create`]: { version: 3 },
	[`POST /v1/table/${TABLE}/tags/delete`]: { version: 5 },
	[`POST /v1/table/${TABLE}/tags/update`]: { version: 5 },
	[`POST /v1/table/${TABLE}/branches/create`]: { version: 5 },
	[`POST /v1/table/${TABLE}/branches/delete`]: { version: 5 },
	[`POST /v1/table/${TABLE}/restore`]: { version: 4 },
	[`POST /v1/table/${TABLE}/create_index`]: { transaction_id: 'ix1' },
	[`POST /v1/table/${TABLE}/create_scalar_index`]: { transaction_id: 'ix1' },
	[`POST /v1/table/${TABLE}/index/id_idx/drop`]: { transaction_id: 'ix2' },
	[`POST /v1/table/${TABLE}/maintenance/preview`]: {
		current_version: 3,
		total_versions: 3,
		eligible_versions: [1],
		protected_tags: { blessed: 2 },
		retention_days: null,
		retain_versions: 2,
	},
	[`POST /v1/table/${TABLE}/maintenance/run`]: {
		ok: true,
		old_versions_removed: 1,
		bytes_removed: 512,
	},
	[`POST /v1/table/${TABLE}/maintenance/compact`]: {
		ok: true,
		fragments_removed: 6,
		fragments_added: 1,
	},
	[`POST /v1/table/${TABLE}/add_columns`]: { version: 4 },
	[`POST /v1/table/${TABLE}/alter_columns`]: { version: 4 },
	[`POST /v1/table/${TABLE}/drop_columns`]: { version: 4 },
	[`POST /v1/table/${TABLE}/update_field_metadata`]: { version: 4 },
	[`POST /v1/table/${TABLE}/schema_metadata/update`]: { version: 4 },
	// backfill is the async native job — its response is a job_id, not a version
	[`POST /v1/table/${TABLE}/backfill_column`]: { job_id: 'j1' },
	[`POST /v1/table/${TABLE}/drop`]: { id: ['db1', 't'] },
	[`POST /v1/table/${TABLE}/deregister`]: {
		id: ['db1', 't'],
		location: 's3://lance-catalog/db1$t',
	},
	[`POST /v1/table/${TABLE}/rename`]: {},
	[`POST /v1/table/${TABLE}/update`]: { updated_rows: 2, version: 4 },
	[`POST /v1/table/${TABLE}/delete`]: { version: 5 },
};

/** The renamed table's detail, for the rename-navigates test. */
const RENAMED_ROUTES: Record<string, unknown> = {
	'POST /v1/table/db1%24t2/describe': { ...DESCRIBE, location: 's3://lance-catalog/db1$t2' },
	'POST /v1/table/db1%24t2/stats': { num_rows: 100, total_bytes: 2048, num_indices: 1 },
	'POST /v1/table/db1%24t2/version/list': { versions: MANIFESTS },
	'POST /v1/table/db1%24t2/tags/list': { tags: {} },
	'POST /v1/table/db1%24t2/branches/list': { branches: {} },
	'POST /v1/table/db1%24t2/index/list': { indexes: [] },
	'POST /v1/table/db1%24t2/policy/describe': { status: 404, body: { detail: 'no policy' } },
};

let token: string;
// scope #6 — the latest producing run(s) for the quality badge; the lineage /api proxy is stubbed
// with this, and it is still a BROWSER-side read.
let producersFixture: Array<Record<string, unknown>>;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

/** The FIRST call to `path`, or undefined — the ledger's answer to "did this write fire, and with
 *  what body?". `undefined` is the assertion for "it must not have fired yet". */
const callTo = async (page: Page, path: string): Promise<Body | undefined> =>
	(await calls(page)).find((c) => c.path === path);

const bodyOf = async (page: Page, path: string): Promise<unknown> =>
	(await callTo(page, path))?.body;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	producersFixture = []; // default: no quality-bearing runs → honest "no quality gate"
	await signIn(context, { token });
	await mockMe(page);
	// The #6 quality badge reads producing runs through the lineage BFF; stub it to stay hermetic.
	await page.route('**/api/datasets/**/producers', (route) =>
		json(route, { producers: producersFixture }),
	);
	await seed(page, {
		...DETAIL_ROUTES,
		...WRITE_ROUTES,
		'GET /v1/table': { tables: ['db1$t', 'db1$other'] },
		[`GET /v1/table/${TABLE}/history`]: HISTORY,
	});
});

/** #113 the version/branch/tag surface lives on the History tab now, not in an overview section. */
async function openHistory(page: Page): Promise<void> {
	await page.getByRole('tab', { name: 'history' }).click();
	await expect(page.locator('section.hist')).toBeVisible();
}

test('the commit log lists every version newest-first, with branches and tags (#66/#113)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await expect(page.getByRole('heading', { name: 'db1$t' })).toBeVisible();
	// The old surface is GONE from overview — one version table in the zone, not two.
	await expect(page.getByText('Versions, branches & tags')).toHaveCount(0);
	await openHistory(page);
	const section = page.locator('section.hist');
	await expect(section.locator('tbody tr').first()).toContainText('v3');
	await expect(section).toContainText('v1');
	// the tag decorates the version it pins, on that row — not in a disconnected chip row far below
	await expect(section.locator('tbody tr', { hasText: 'v2' }).first()).toContainText('blessed');
	// branches + tags sections moved with it
	await expect(section).toContainText('main');
	await expect(section).toContainText('blessed → v2');
	// indexes stay on overview (#64)
	await page.getByRole('tab', { name: 'overview' }).click();
	const indexes = page.locator('section', { hasText: 'Indexes' });
	await expect(indexes).toContainText('id_idx');
	await expect(indexes).toContainText('BTREE');
});

test('tagging is a per-row action on the selected commit (#64/#113)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	// "pin THIS version" — select the commit, then name it. No version dropdown to get wrong.
	await page.getByRole('button', { name: 'details for v3', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	await panel.getByLabel('Tag name').fill('release-1');
	await panel.getByRole('button', { name: 'Tag v3' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/tags/create`))
		.toEqual({ tag: 'release-1', version: 3 });
});

test('restore needs the version TYPED before it posts {version} (#64/#113)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	await page.getByRole('button', { name: 'details for v2', exact: true }).click();
	const panel = page.getByTestId('commit-panel');
	// The label says what a restore actually does — it mints a NEW version, it does not rewrite history.
	await panel.getByRole('button', { name: 'Restore v2 (creates a new version)' }).click();
	// Arming is not confirming, and the confirm stays disabled until the identifier matches.
	expect(await callTo(page, `/v1/table/${TABLE}/restore`)).toBeUndefined();
	await expect(panel.getByRole('button', { name: 'Confirm restore' })).toBeDisabled();
	await panel.getByLabel('Type the version to confirm the restore').fill('v9');
	await expect(panel.getByRole('button', { name: 'Confirm restore' })).toBeDisabled();
	await panel.getByLabel('Type the version to confirm the restore').fill('v2');
	await panel.getByRole('button', { name: 'Confirm restore' }).click();
	await expect.poll(() => bodyOf(page, `/v1/table/${TABLE}/restore`)).toEqual({ version: 2 });
});

test('insert-rows form encodes JSON to an Arrow-IPC body and posts to /insert (#64)', async ({
	page,
}) => {
	// The insert keeps its bytes route (Arrow IPC in), so it is still a BROWSER call — page.route sees
	// it, and the body must be non-empty binary, never JSON.
	let insertPostBytes = 0;
	await page.route('**/capi/v1/table/*/insert*', (route) => {
		insertPostBytes = route.request().postDataBuffer()?.length ?? 0;
		return json(route, { transaction_id: 'tx1' });
	});
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Insert rows' });
	await section.locator('textarea.ins').fill('[{ "id": 9, "name": "z" }]');
	await section.getByRole('button', { name: 'Insert' }).click();
	await expect.poll(() => insertPostBytes).toBeGreaterThan(0);
	await expect(section).toContainText('Inserted 1 row');
});

test('builds a vector index through the create form (#73)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Indexes' });
	await section.getByLabel('Index column').fill('vec');
	await section.getByLabel('Index type').click();
	await page.getByRole('option', { name: 'vector · IVF_PQ' }).click();
	await section.getByRole('button', { name: 'Build index' }).click();
	// A vector type routes to create_index and carries the distance type. The scalar/vector choice is
	// the ENDPOINT now (it used to be a `?scalar=` flag on this zone's route), so the wire says it.
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/create_index`))
		.toEqual({ column: 'vec', index_type: 'IVF_PQ', distance_type: 'cosine' });
	expect(await callTo(page, `/v1/table/${TABLE}/create_scalar_index`)).toBeUndefined();
});

test('drops an index via the chip × (#73)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Indexes' });
	await section.getByRole('button', { name: 'drop index id_idx' }).click();
	await expect.poll(() => callTo(page, `/v1/table/${TABLE}/index/id_idx/drop`)).toBeDefined();
});

test('GC preview lists reclaimable versions + protected tags (#75)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Maintenance policy' });
	await section.getByRole('spinbutton', { name: 'keep last' }).fill('2');
	await section.getByRole('button', { name: 'Preview' }).click();
	await expect(section.locator('.gc')).toContainText('1 version reclaimable');
	await expect(section.locator('.gc')).toContainText('blessed→v2'); // tag protection surfaced
	expect(await bodyOf(page, `/v1/table/${TABLE}/maintenance/preview`)).toEqual({
		retention_days: null,
		retain_versions: 2,
	});
});

test('GC reclaim is a two-click confirm and posts to /maintenance/run (#75)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const gc = page.locator('section', { hasText: 'Maintenance policy' }).locator('.gc');
	await gc.getByRole('spinbutton', { name: 'keep last' }).fill('2');
	await gc.getByRole('button', { name: 'Preview' }).click();
	await gc.getByRole('button', { name: 'Reclaim now' }).click();
	// first click only arms the confirm — no run yet
	expect(await callTo(page, `/v1/table/${TABLE}/maintenance/run`)).toBeUndefined();
	await gc.getByRole('button', { name: 'Confirm reclaim' }).click();
	await expect.poll(() => callTo(page, `/v1/table/${TABLE}/maintenance/run`)).toBeDefined();
	await expect(gc).toContainText('Reclaimed 1 version');
});

test('compact-now posts to /maintenance/compact with the policy target size (#76)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const gc = page.locator('section', { hasText: 'Maintenance policy' }).locator('.gc');
	await gc.getByRole('button', { name: 'Compact now' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/maintenance/compact`))
		.toEqual({ target_rows_per_fragment: 1048576 });
	await expect(gc).toContainText('6 fragment'); // "Compacted · 6 fragment(s) → 1."
});

test('the policy surfaces the compaction target size (#76)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Maintenance policy' });
	await expect(section).toContainText('target 1048576 rows/frag');
});

test('add-column posts a SQL-expression column (#74)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByLabel('New column name').fill('doubled');
	await section.getByLabel('Column SQL expression').fill('id * 2');
	await section.getByRole('button', { name: 'Add column' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/add_columns`))
		.toEqual({ new_columns: [{ name: 'doubled', expression: 'id * 2' }] });
});

test('drop-column via the row × posts {columns} (#74)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByRole('button', { name: 'drop id' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/drop_columns`))
		.toEqual({ columns: ['id'] });
});

test('rename-column via the row ✎ posts an alter path→rename (#74)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByRole('button', { name: 'rename id' }).click();
	await section.getByLabel('rename id to').fill('identifier');
	await section.getByRole('button', { name: 'save' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/alter_columns`))
		.toEqual({ alterations: [{ path: 'id', rename: 'identifier' }] });
});

test('re-type-column via the row ⇄ posts an alter path→data_type (#74 tail)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByRole('button', { name: 're-type id' }).click();
	// the target type is the @rask/ui Select (bits-ui) — open it, pick float32
	await section.getByLabel('re-type id to').click();
	await page.getByRole('option', { name: 'float32', exact: true }).click();
	await section.getByRole('button', { name: 'save' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/alter_columns`))
		.toEqual({ alterations: [{ path: 'id', data_type: { type: 'float32' } }] });
});

test('save table properties posts every live key (#74 tail)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Properties' });
	// the seed row is describe.metadata { owner: "data-eng" }; add a second key, then save
	await section.getByRole('button', { name: '+ add row' }).click();
	await section.getByLabel('Property key 2').fill('tier');
	await section.getByLabel('Property value 2').fill('gold');
	await section.getByRole('button', { name: 'Save properties' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/schema_metadata/update`))
		.toEqual({ metadata: { owner: 'data-eng', tier: 'gold' } });
	await expect(section.getByText('Saved.')).toBeVisible();
});

test('removing a property row posts a null-valued key (#78)', async ({ page }) => {
	// The × used to just drop the row from the local map — and since the write MERGES, the key stayed on
	// the table while the UI said "Saved." Removal has to be SAID, not implied by omission.
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Properties' });
	await section.getByRole('button', { name: 'Remove property 1' }).click();
	await section.getByRole('button', { name: 'Save properties' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/schema_metadata/update`))
		.toEqual({ metadata: { owner: null } });
});

test('the description is a first-class field and renders under the table name (#78)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Properties' });
	await section.getByLabel('Table description').fill('Page images, one row per scan.');
	await section.getByRole('button', { name: 'Save properties' }).click();
	// ONE post carries the description alongside the untouched rows — a separate call would be a second
	// Lance version for a single edit.
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/schema_metadata/update`))
		.toEqual({ metadata: { owner: 'data-eng', description: 'Page images, one row per scan.' } });

	// and the detail header reads it back (read-only there — the editor is the one door). `seed` merges
	// per key, so overriding `describe` alone leaves the rest of the fixture standing.
	await seed(page, {
		[`POST /v1/table/${TABLE}/describe`]: {
			...DESCRIBE,
			metadata: { owner: 'data-eng', description: 'Page images, one row per scan.' },
		},
	});
	await page.reload();
	await expect(page.locator('p.tdesc')).toHaveText('Page images, one row per scan.');
});

test('set a column property merges one key (#74 tail)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Properties' });
	await section.getByLabel('Column for properties').click();
	await page.getByRole('option', { name: 'id', exact: true }).click();
	// the seeded field metadata { unit: "count" } renders as a deletable chip
	await expect(section).toContainText('unit=count');
	await section.getByLabel('Column property key').fill('pii');
	await section.getByLabel('Column property value').fill('false');
	await section.getByRole('button', { name: 'Set', exact: true }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/update_field_metadata`))
		.toEqual({ updates: [{ path: 'id', metadata: { pii: 'false' }, replace: false }] });
});

test('delete a column property posts a null-valued key (#74 tail)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Properties' });
	await section.getByLabel('Column for properties').click();
	await page.getByRole('option', { name: 'id', exact: true }).click();
	await section.getByRole('button', { name: 'Delete column property unit' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/update_field_metadata`))
		.toEqual({ updates: [{ path: 'id', metadata: { unit: null }, replace: false }] });
});

test('deleting a tag asks first, then posts (#74/#113)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	const section = page.locator('section.hist');
	await section.getByRole('button', { name: 'delete tag blessed' }).click();
	// The bare × used to delete a ref on ONE click, with no confirmation at all.
	expect(await callTo(page, `/v1/table/${TABLE}/tags/delete`)).toBeUndefined();
	await section.getByRole('button', { name: 'confirm delete tag blessed' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/tags/delete`))
		.toEqual({ tag: 'blessed' });
});

test('move a tag to another version (#74)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	const section = page.locator('section.hist');
	await section.getByRole('button', { name: 'move tag blessed' }).click();
	await section.getByLabel('move blessed to version').click();
	await page.getByRole('option', { name: 'v3', exact: true }).click();
	await section.getByRole('button', { name: 'save' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/tags/update`))
		.toEqual({ tag: 'blessed', version: 3 });
});

test('create a branch from a version (#74)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	const section = page.locator('section.hist');
	await section.getByLabel('New branch name').fill('feature');
	await section.getByLabel('Branch from version').click();
	await page.getByRole('option', { name: 'v2', exact: true }).click();
	await section.getByRole('button', { name: 'Create branch' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/branches/create`))
		.toEqual({ name: 'feature', from_version: 2 });
});

test('deleting a branch asks first, then posts (#74/#113)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await openHistory(page);
	const section = page.locator('section.hist');
	await section.getByRole('button', { name: 'delete branch dev' }).click();
	expect(await callTo(page, `/v1/table/${TABLE}/branches/delete`)).toBeUndefined();
	await section.getByRole('button', { name: 'confirm delete branch dev' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/branches/delete`))
		.toEqual({ name: 'dev' });
});

test('surfaces the Lance file format badge (#78)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const stats = page.locator('section', { hasText: 'Stats' }).first();
	await expect(stats).toContainText('Lance · storage v2.2');
});

test('surfaces the validator quality gate when a run passed it (#82)', async ({ page }) => {
	producersFixture = [
		{ run_id: 'r1', quality_passed: true, quality_assertions: [{ assertion: 'row_count' }] },
	];
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const stats = page.locator('section', { hasText: 'Stats' }).first();
	await expect(stats).toContainText('quality passed · 1 check');
});

test('surfaces a blocked quality gate (#82)', async ({ page }) => {
	producersFixture = [
		{
			run_id: 'r1',
			quality_passed: false,
			quality_assertions: [{ assertion: 'not_null' }, { assertion: 'unique' }],
		},
	];
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const stats = page.locator('section', { hasText: 'Stats' }).first();
	await expect(stats).toContainText('quality blocked · 2 checks');
});

test("states 'no quality gate' honestly when no run recorded assertions (#82)", async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t'); // producersFixture defaults to []
	const stats = page.locator('section', { hasText: 'Stats' }).first();
	await expect(stats).toContainText('no quality gate');
});

// --- #85 danger zone: drop / deregister (AlertDialog confirm) + rename (navigates) ---

test('danger-zone drop confirms via the AlertDialog, closes it, and the registry row is gone (#85)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	// the post-drop world the registry re-lists after navigation
	await seed(page, { 'GET /v1/table': { tables: ['db1$other'] } });
	const danger = page.locator('section.dangerzone');
	await danger.getByRole('button', { name: 'Drop table' }).click();
	// The confirm is the portalled @rask/ui AlertDialog — drive it by role, not the trigger section.
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Drop table db1$t');
	await dialog.getByRole('button', { name: 'Drop', exact: true }).click();
	// The exact wire path, %24-encoded id included — poll, don't race the request.
	await expect.poll(() => callTo(page, `/v1/table/${TABLE}/drop`)).toBeDefined();
	// The dialog must CLOSE after the drop — a still-open dialog keeps the destructive action armed
	// for a second, confirm-free fire (the NamespaceRegistry audit fix, copied here).
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	// The id no longer names a table — back to the registry, where the dropped row is gone.
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables$/);
	await expect(page.getByRole('link', { name: 'db1$other' })).toBeVisible();
	await expect(page.getByRole('link', { name: 'db1$t', exact: true })).toHaveCount(0);
});

test('danger-zone deregister confirms via its own AlertDialog copy (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await page.locator('section.dangerzone').getByRole('button', { name: 'Deregister' }).click();
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toContainText('Deregister table db1$t');
	await expect(dialog).toContainText('data stays on storage'); // deregister ≠ drop, stated honestly
	await dialog.getByRole('button', { name: 'Deregister', exact: true }).click();
	await expect.poll(() => callTo(page, `/v1/table/${TABLE}/deregister`)).toBeDefined();
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables$/);
});

test('cancelling the danger-zone confirm never posts (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	await page.locator('section.dangerzone').getByRole('button', { name: 'Drop table' }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Cancel' }).click();
	await expect(page.getByRole('alertdialog')).toHaveCount(0);
	expect(await callTo(page, `/v1/table/${TABLE}/drop`)).toBeUndefined();
	// still on the detail page — nothing was dropped
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables\/db1%24t$/);
});

test('a 403 drop surfaces the owner-denied state and stays on the page (#85)', async ({ page }) => {
	// Deny like the catalog's FGA gate (can_drop) does for a non-owner.
	await seed(page, {
		[`POST /v1/table/${TABLE}/drop`]: { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const danger = page.locator('section.dangerzone');
	await danger.getByRole('button', { name: 'Drop table' }).click();
	await page.getByRole('alertdialog').getByRole('button', { name: 'Drop', exact: true }).click();
	await expect(danger).toContainText('Denied: drop needs the owner rung (can_drop).');
	await expect(page.getByRole('alertdialog')).toHaveCount(0); // closed even on failure (finally)
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables\/db1%24t$/);
});

test('rename posts {new_table_name} and navigates to the renamed detail (#85)', async ({
	page,
}) => {
	await seed(page, RENAMED_ROUTES);
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const danger = page.locator('section.dangerzone');
	await danger.getByLabel('Rename table to').fill('t2');
	await danger.getByRole('button', { name: 'Rename' }).click();
	// The exact wire body: the catalog's RenameTableRequest carries new_table_name (namespace kept).
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/rename`))
		.toEqual({ new_table_name: 't2' });
	// success navigates to the renamed table's detail (the old id no longer exists)
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables\/db1%24t2$/);
	await expect(page.getByRole('heading', { name: 'db1$t2' })).toBeVisible();
});

test('a 409 rename surfaces "already exists" and stays on the page (#85)', async ({ page }) => {
	// The catalog answers 409 when the destination id is taken — the page must say so (not the generic
	// detail passthrough) and must NOT navigate: the source table still exists under its old id.
	await seed(page, {
		[`POST /v1/table/${TABLE}/rename`]: { status: 409, body: { detail: 'conflict' } },
	});
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const danger = page.locator('section.dangerzone');
	await danger.getByLabel('Rename table to').fill('other');
	await danger.getByRole('button', { name: 'Rename' }).click();
	await expect(danger).toContainText('Denied: a table named db1$other already exists.');
	await expect(page).toHaveURL(/\/lakehouse\/catalog\/tables\/db1%24t$/);
	await expect(page.getByRole('heading', { name: 'db1$t' })).toBeVisible();
});

// --- #85 row ops: update / delete by SQL predicate ---

test('update rows posts the exact {predicate, updates} wire body and surfaces the count (#85)', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Update / delete rows' });
	await section.getByLabel('Row predicate').fill('id > 3');
	await section.getByLabel('SET column 1').fill('name');
	await section.getByLabel('SET expression 1').fill("'x'");
	await section.getByRole('button', { name: 'Update rows' }).click();
	// UpdateTableRequest on the wire: updates = [[column, expression], …] pairs + the predicate.
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/update`))
		.toEqual({ predicate: 'id > 3', updates: [['name', "'x'"]] });
	// updated_rows + version are REQUIRED on the wire — the affected-row count surfaces
	await expect(section).toContainText('Updated 2 rows → v4.');
});

test('a partially-filled SET pair blocks the update with an inline hint (#85)', async ({
	page,
}) => {
	// A half-filled pair must BLOCK, not be silently dropped — else the write that fires differs from
	// the form on screen (audit 2026-07-23).
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Update / delete rows' });
	const update = section.getByRole('button', { name: 'Update rows' });
	await section.getByLabel('SET column 1').fill('name');
	// column without expression → disabled + the hint names the problem
	await expect(update).toBeDisabled();
	await expect(section).toContainText('A SET pair is only half-filled');
	// completing the pair clears the block; nothing was ever posted while blocked
	await section.getByLabel('SET expression 1').fill("'x'");
	await expect(update).toBeEnabled();
	await expect(section.getByText('A SET pair is only half-filled')).toHaveCount(0);
	expect(await callTo(page, `/v1/table/${TABLE}/update`)).toBeUndefined();
	// a SECOND, half-filled pair re-blocks even though pair 1 is complete
	await section.getByRole('button', { name: '+ add SET pair' }).click();
	await section.getByLabel('SET expression 2').fill('true');
	await expect(update).toBeDisabled();
	await expect(section).toContainText('A SET pair is only half-filled');
});

test('update with an empty predicate omits the key (all rows) (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Update / delete rows' });
	await section.getByLabel('SET column 1').fill('flag');
	await section.getByLabel('SET expression 1').fill('true');
	await section.getByRole('button', { name: 'Update rows' }).click();
	// no predicate key at all — an empty-string predicate is not the same wire contract
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/update`))
		.toEqual({ updates: [['flag', 'true']] });
});

test('delete rows is a two-click confirm and posts {predicate} only (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Update / delete rows' });
	await section.getByLabel('Row predicate').fill('id = 9');
	await section.getByRole('button', { name: 'Delete rows' }).click();
	// first click only arms the confirm — no write yet
	expect(await callTo(page, `/v1/table/${TABLE}/delete`)).toBeUndefined();
	await section.getByRole('button', { name: 'confirm delete' }).click();
	// DeleteFromTableRequest on the wire: predicate REQUIRED, nothing else
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/delete`))
		.toEqual({ predicate: 'id = 9' });
	// the delete wire has no row count — the new version is surfaced honestly instead
	await expect(section).toContainText('Deleted rows matching the predicate → v5.');
});

test('a 403 row update renders the writer-denied state (#85)', async ({ page }) => {
	await seed(page, {
		[`POST /v1/table/${TABLE}/update`]: { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Update / delete rows' });
	await section.getByLabel('SET column 1').fill('name');
	await section.getByLabel('SET expression 1').fill("'x'");
	await section.getByRole('button', { name: 'Update rows' }).click();
	await expect(section).toContainText('Denied: row changes need writer access (can_write_data).');
});

// --- #85 backfill_column: the async columns op beside add/alter/drop ---

test('backfill posts {column, where} and surfaces the job id (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByRole('button', { name: 'backfill id' }).click();
	await section.getByLabel('backfill id where').fill('id > 0');
	await section.getByRole('button', { name: 'run' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/backfill_column`))
		.toEqual({ column: 'id', where: 'id > 0' });
	// async job — the UI surfaces the job_id (no version to refresh yet)
	await expect(section).toContainText('Backfill of id started · job j1.');
});

test('backfill with no predicate omits the where key (#85)', async ({ page }) => {
	await page.goto('/lakehouse/catalog/tables/db1%24t');
	const section = page.locator('section', { hasText: 'Schema' }).first();
	await section.getByRole('button', { name: 'backfill id' }).click();
	await section.getByRole('button', { name: 'run' }).click();
	await expect
		.poll(() => bodyOf(page, `/v1/table/${TABLE}/backfill_column`))
		.toEqual({ column: 'id' });
});

// --------------------------------------------------------------------------- //
// #75 recovery — the undrop surface. It shipped with a `data-testid` and no test
// at all: a destructive-recovery affordance, racing a real expiry deadline,
// reachable only from a dropped table's 404, and never once driven.
// --------------------------------------------------------------------------- //

const GONE = 'db1%24dropped';
const TRASH_ENTRY = {
	id: 'db1$dropped',
	location: 's3://lance-catalog/db1$dropped',
	dropped_by: 'CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0MzESBWxvY2Fs',
	dropped_at: '2026-08-01T09:15:00Z',
	expires_at: '2026-08-08T09:15:00Z',
};

/** A dropped table: `describe` 404s (it is deregistered) but a trash record still exists. */
const droppedRoutes = (entry: unknown = TRASH_ENTRY): Record<string, unknown> => ({
	[`POST /v1/table/${GONE}/describe`]: { status: 404, body: { detail: 'Table not found' } },
	[`GET /v1/table/${GONE}/tasks`]: entry === null ? [] : [entry],
});

test('a dropped-but-recoverable table offers undrop, with the real deadline (#75)', async ({
	page,
}) => {
	await seed(page, droppedRoutes());
	await page.goto(`/lakehouse/catalog/tables/${GONE}`);

	const panel = page.locator('.recover');
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('still recoverable');
	// The location is what recovery re-registers — it must be shown, not implied.
	await expect(panel).toContainText('s3://lance-catalog/db1$dropped');
	// The deadline is the whole point of the surface: a date the user can act before.
	await expect(panel).toContainText('2026-08-08');
	// …and the dropper goes through <Subject> (#68/#87): the opaque dex sub is ellipsized rather
	// than dumped as a 60-character run-on blob, with the FULL value kept in `title` — the form a
	// tuple write actually needs. There is no identity directory to resolve it against, so this
	// typographic contract IS the guarantee.
	await expect(panel).not.toContainText(TRASH_ENTRY.dropped_by);
	await expect(panel.locator(`[title="${TRASH_ENTRY.dropped_by}"]`)).toBeVisible();
});

test('an unreadable trash record is NAMED, never rendered as "your data is gone" (#147)', async ({
	page,
}) => {
	// The branch that had no coverage at all, and the reason this card was wrong: a FAILED read used
	// to render the same "not a catalog-registered table" copy as a genuine absence. The catalog
	// answers 200 with an empty list when there really is no record, so a non-2xx here is strictly an
	// anomaly — and an anomaly must not be reported to the owner of a recoverable table as a verdict.
	await seed(page, {
		[`POST /v1/table/${GONE}/describe`]: { status: 404, body: { detail: 'Table not found' } },
		[`GET /v1/table/${GONE}/tasks`]: { status: 500, body: { detail: 'trash store unreachable' } },
	});
	await page.goto(`/lakehouse/catalog/tables/${GONE}`);

	const panel = page.locator('.empty.stack');
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('Could not read this table');
	// The upstream's own words, VERBATIM — a synthesised "HTTP {status}" would print "HTTP 0" for the
	// most likely failure of all (an unreachable catalog).
	await expect(panel).toContainText('trash store unreachable');
	// …and it must NOT claim the drop was destructive.
	await expect(panel).not.toContainText('Not a catalog-registered table');
});

test('undrop posts to the catalog and re-registers the table (#75)', async ({ page }) => {
	await seed(page, {
		...droppedRoutes(),
		[`POST /v1/table/${GONE}/undrop`]: { id: ['db1', 'dropped'] },
	});
	await page.goto(`/lakehouse/catalog/tables/${GONE}`);
	await expect(page.locator('.recover')).toBeVisible();

	// The write must not have fired before the click — this is a recovery, not an auto-heal.
	expect(await callTo(page, `/v1/table/${GONE}/undrop`)).toBeUndefined();
	await page.getByRole('button', { name: 'Undrop this table' }).click();
	await expect.poll(async () => await callTo(page, `/v1/table/${GONE}/undrop`)).not.toBeUndefined();
});

test('a REFUSED undrop shows the catalog reason verbatim and keeps the panel (#75)', async ({
	page,
}) => {
	await seed(page, {
		...droppedRoutes(),
		[`POST /v1/table/${GONE}/undrop`]: {
			status: 403,
			body: { detail: 'can_delete required on table:db1$dropped' },
		},
	});
	await page.goto(`/lakehouse/catalog/tables/${GONE}`);
	await page.getByRole('button', { name: 'Undrop this table' }).click();

	// Verbatim: an expired grace period reads differently from a permission denial, and the user
	// has to be able to tell which one they hit.
	await expect(page.getByTestId('undrop-problem')).toContainText('can_delete required');
	// The panel SURVIVES the refusal — a failed recovery must not hide the way to retry it.
	await expect(page.locator('.recover')).toBeVisible();
});

test('a table dropped DESTRUCTIVELY offers no recovery it cannot deliver (#75)', async ({
	page,
}) => {
	await seed(page, droppedRoutes(null)); // no trash record: the bytes are gone
	await page.goto(`/lakehouse/catalog/tables/${GONE}`);

	await expect(page.locator('.recover')).toHaveCount(0);
	await expect(page.getByText('Not a catalog-registered table')).toBeVisible();
});

// NOT TESTED HERE, deliberately: "a live table never probes /tasks". The recovery probe is gated
// on a 404 (TableDetail.svelte), and that gating is real — but this harness cannot FALSIFY it. The
// mock ledger records a route only when it serves one, and every attempt to make the negative bite
// (seeding the path, removing the `notInCatalog` guard from the component) left the test green. An
// assertion that survives the removal of the thing it asserts is decoration, so it is gone rather
// than sitting here looking like coverage. Closing it needs a ledger that records unmatched
// requests too — a harness change, not a spec one.
