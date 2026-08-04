import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG, MOCK_OBS } from '../ports';

// Hermetic /models coverage. The registry's reads and its promote ride `models.remote.ts` now, so
// they run on the ZONE SERVER and `page.route` cannot reach them — the responses are seeded on the
// mock catalog per bearer instead (the admin dev server's CATALOG_API points there), and the promote's
// wire body is read back from the mock's call log.
//
// The training curves moved the same way: `TrainingCurves.svelte` calls `fetchTrainingCurves`
// (`models/remote/experiments.remote.ts`), which runs REAL PromQL range queries against GREPTIME_API
// server-side. The `/api/experiments` BFF route it replaced no longer exists, so the `page.route`
// stand-in this file used to carry intercepted nothing and every curve read fell through to an
// unseeded mock → 404 → the "metrics store unreachable" state. They are seeded on
// mock-observability now, like models-experiments.spec.ts does for the instant queries.
//
// Lives in the admin project because that is the server whose CATALOG_API is the mock; each test signs
// in with its own bearer so the fullyParallel suite shares no mock state.

type Model = { model: string; latest_version: number | null; blessed_version: number | null };

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

/** Same, on the GreptimeDB stand-in the curve reads reach. */
const seedObs = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_OBS}/__mock/seed`, { data: { bearer: token, routes } });
};

// `promqlRange` stamps `start`/`end` from the SERVER's clock, so the query string cannot be predicted
// from here — the seed is keyed on the PATH alone, which mock-observability falls back to when no
// with-query key matches. One consequence is load-bearing for the test below: all three curve queries
// (rows / features / runs) and both models share that one key, so "this curve has points and that one
// does not" has to be staged in TIME (re-seed between reads) rather than per query.
const CURVE_RANGE = 'GET /v1/prometheus/api/v1/query_range';

/** A raw Prometheus range (matrix) body — exactly what GreptimeDB's query_range answers.
 *  `status` is the STRING "success": mock-observability detects its {status, body} envelope by a
 *  NUMERIC status only, precisely so a Prometheus payload passes through as a body instead of being
 *  read as "respond 'success'". */
const promRange = (points: Array<[number, number]>) => ({
	status: 'success',
	data: {
		resultType: 'matrix',
		result: points.length
			? [
					{
						metric: {},
						values: points.map(([t, v]) => [t, String(v)] as [number, string]),
					},
				]
			: [],
	},
});

/** Every mutating request this test's bearer made to the catalog. */
const calls = async (
	page: Page,
): Promise<Array<{ method: string; path: string; body: unknown }>> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Array<{ method: string; path: string; body: unknown }> })
		.calls;
};

const describeOf = (m: Model, artifacts: unknown[]) => ({
	model: m.model,
	latest_version: m.latest_version ?? 1,
	blessed_version: m.blessed_version,
	candidate_metrics: { rows_seen: 9, loss: 0.1234 },
	blessed_metrics: m.blessed_version ? { rows_seen: 4, loss: 0.5 } : null,
	// The frozen contract's new field: the models/<model>/ object listing.
	artifacts,
});

const DEMO_ARTIFACTS = [
	{ path: '3/weights.json', size_bytes: 2048, updated_at: '2026-07-24T09:00:00Z' },
	{ path: '3/scaler.json', size_bytes: 512, updated_at: null },
];

const DEMO: Model = { model: 'demo', latest_version: 3, blessed_version: 2 };
const FRAUD: Model = { model: 'fraud', latest_version: 1, blessed_version: null };

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	// Default: the metrics store is up and has NOTHING for these models. Every detail-panel test
	// expands a row, so without this each one would render "metrics store unreachable" — a state that
	// is not what it is testing, and one that used to be masked by the dead `/api/experiments` route.
	await seedObs(page, { [CURVE_RANGE]: promRange([]) });
	await seed(page, {
		'GET /v1/model': { models: [DEMO, FRAUD] },
		'GET /v1/model/demo': describeOf(DEMO, DEMO_ARTIFACTS),
		'GET /v1/model/fraud': describeOf(FRAUD, []),
	});
});

test('registry list renders candidate/blessed state per model', async ({ page }) => {
	await page.goto('/lakehouse/models');
	await expect(page.getByRole('heading', { name: 'Model registry' })).toBeVisible();
	const demoRow = page.locator('tr', { hasText: 'demo' }).first();
	await expect(demoRow).toContainText('v3');
	await expect(demoRow).toContainText('blessed behind');
	const fraudRow = page.locator('tr', { hasText: 'fraud' }).first();
	await expect(fraudRow).toContainText('candidate only');
});

test('clicking a model opens the candidate-vs-blessed metrics panel', async ({ page }) => {
	await page.goto('/lakehouse/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	await expect(page.locator('.metrics')).toBeVisible();
	await expect(page.locator('.metrics')).toContainText('rows_seen');
	await expect(page.locator('.metrics')).toContainText('0.1234'); // candidate loss
	await expect(page.locator('.metrics')).toContainText('0.5000'); // blessed loss
});

test('the detail lists the artifacts as a sortable table', async ({ page }) => {
	await page.goto('/lakehouse/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	const artifacts = page.getByLabel('Artifacts for demo');
	await expect(artifacts).toContainText('3/weights.json');
	await expect(artifacts).toContainText('2.0 KiB'); // size_bytes rendered human-readable
	await expect(artifacts).toContainText('3/scaler.json');
	await expect(artifacts).toContainText('—'); // null updated_at renders honestly
});

test('an artifact-less model shows the honest empty artifacts state', async ({ page }) => {
	await page.goto('/lakehouse/models');
	await page.locator('td', { hasText: 'fraud' }).first().click();
	await expect(page.getByLabel('Artifacts for fraud')).toContainText('No artifacts listed');
});

test('training curves plot where series exist and state the truth where none do', async ({
	page,
}) => {
	// demo reads while the range query answers with a real matrix → its curves plot for real, from
	// the flattened [unix, value] pairs `promqlRange` produces (the seeded body is the raw Prometheus
	// answer, so the query URL, the parse and the LayerChart render are all exercised).
	await seedObs(page, {
		[CURVE_RANGE]: promRange([
			[1753351200, 4],
			[1753354800, 9],
		]),
	});
	await page.goto('/lakehouse/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	await expect(page.getByLabel('Curve Rows seen per run')).toBeVisible();
	await expect(
		page.getByLabel('Curve Rows seen per run').locator('svg.lc-layout-svg'),
	).toBeVisible();
	// All three of the remote function's curves are named and plotted — the titles are the contract
	// between experiments.remote.ts's CURVES table and the figures the detail renders.
	await expect(page.getByLabel(/^Curve /)).toHaveCount(3);

	// fraud reads against an EMPTY matrix → the honest empty state, and NOT a fabricated flat line:
	// `withData` drops every point-less curve, so no figure survives.
	//
	// The old assertion here ("Curve Cumulative training runs" absent WHILE the rows curve plots)
	// cannot be expressed any more and is deliberately not faked: the curve reads are server-side
	// PromQL now, and mock-observability can only key a `query_range` seed on the path — its
	// `start`/`end` come from the server clock — so all three curve queries necessarily share one
	// answer. The point-less-curve filter is pinned below at the all-empty granularity instead; the
	// mixed case needs mock-observability to match seeds on the `query` parameter.
	await seedObs(page, { [CURVE_RANGE]: promRange([]) });
	await page.locator('td', { hasText: 'demo' }).first().click(); // collapse
	await page.locator('td', { hasText: 'fraud' }).first().click();
	await expect(page.getByText('No training series recorded for this model')).toBeVisible();
	await expect(page.getByLabel(/^Curve /)).toHaveCount(0);
});

test('bless promotes the candidate and the row updates', async ({ page }) => {
	await page.goto('/lakehouse/models');
	const demoRow = page.locator('tr', { hasText: 'demo' }).first();
	const bless = demoRow.getByRole('button', { name: 'bless v3' });
	await expect(bless).toBeVisible();
	// The catalog's world AFTER the promote. Seeded once the pre-promote list is on screen, so the
	// single-flight re-read the command fires lands on the moved pointer — the mock is a fixed
	// response per key, so "what changed" has to be stated rather than simulated.
	const blessed: Model = { ...DEMO, blessed_version: 3 };
	await seed(page, {
		'POST /v1/model/demo/promote': { model: 'demo', blessed_version: 3, tag: 'blessed' },
		'GET /v1/model': { models: [blessed, FRAUD] },
		'GET /v1/model/demo': describeOf(blessed, DEMO_ARTIFACTS),
	});
	await bless.click();
	await expect(page.locator('.banner.ok')).toContainText('demo v3 is now blessed');
	await expect(demoRow).toContainText('blessed'); // state chip converges after the refetch
	await expect(demoRow.getByRole('button')).toHaveCount(0); // nothing left to bless
	// the wire body is the version, on the model's own promote path
	expect(await calls(page)).toEqual([
		{ method: 'POST', path: '/v1/model/demo/promote', body: { version: 3 } },
	]);
});

test('the shared sidebar marks Registry active and links to Lineage as a same-zone soft nav', async ({
	page,
}) => {
	await page.goto('/lakehouse/models');
	// On the models zone root the Registry leaf is active (exact-match — not lit on sibling sub-routes).
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Registry' })).toBeVisible();
	// Lineage used to be a DIFFERENT zone, so these links had to force a full-document reload. It is
	// an AREA of this same zone now: the route manifest already contains them, so they must soft-nav.
	// And it is a COLUMN of Lakehouse's panel, not a trigger of its own — reached via that trigger.
	await page
		.getByRole('navigation', { name: 'Zones' })
		.getByRole('button', { name: 'Lakehouse' })
		.click();
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	// The area root appears exactly once (lineage's Graph row IS /lakehouse/lineage — no duplicate).
	await expect(panel.locator('a[href="/lakehouse/lineage"]')).toHaveCount(1);
	await expect(panel.locator('a[href="/lakehouse/lineage"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
	await expect(panel.locator('a[href="/lakehouse/lineage/datasets"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});
