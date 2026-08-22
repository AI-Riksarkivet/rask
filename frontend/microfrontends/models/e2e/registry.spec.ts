import { test, expect, type Page } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { closePanel, openPanel } from './nav';
import { MOCK_UPSTREAMS } from './ports';

// Hermetic registry coverage for the ZONE ROOT (`/models`). Moved here from
// `lakehouse/e2e/admin/models-registry.spec.ts` with the route itself.
//
// Every read on this page runs on the ZONE SERVER, so `page.route` reaches none of it: the registry's list,
// describe and promote ride `models.remote.ts` against CATALOG_API, and the detail's training curves ride
// `experiments.remote.ts`, which issues REAL PromQL range queries against GREPTIME_API. Both env vars point
// at the one seed-driven mock (playwright.config.ts), so the responses are staged per bearer and the
// promote's wire body is read back from the mock's call log.
//
// Each test signs in with its OWN bearer, so the fullyParallel suite shares no mock state.

type Model = { model: string; latest_version: number | null; blessed_version: number | null };

let token: string;

/** Pre-seed exact upstream responses for THIS test's bearer ("METHOD /path" → body). One helper for both
 *  upstreams: the catalog's `/v1/model*` and Greptime's `/v1/prometheus/*` never collide, so they share a
 *  server and therefore a seed map (see e2e/mock-upstreams.ts). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_UPSTREAMS}/__mock/seed`, { data: { bearer: token, routes } });
};

// `promqlRange` stamps `start`/`end` from the SERVER's clock, so the query string cannot be predicted from
// here — the seed is keyed on the PATH alone, which the mock falls back to when no with-query key matches.
// One consequence is load-bearing for the test below: all three curve queries (rows / features / runs) and
// both models share that one key, so "this curve has points and that one does not" has to be staged in
// TIME (re-seed between reads) rather than per query.
const CURVE_RANGE = 'GET /v1/prometheus/api/v1/query_range';

/** A raw Prometheus range (matrix) body — exactly what GreptimeDB's query_range answers. `status` is the
 *  STRING "success": the mock detects its {status, body} envelope by a NUMERIC status only, precisely so a
 *  Prometheus payload passes through as a body instead of being read as "respond 'success'". */
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

/** Every mutating request this test's bearer made to the upstream. */
const calls = async (
	page: Page,
): Promise<Array<{ method: string; path: string; body: unknown }>> => {
	const res = await page.request.get(`${MOCK_UPSTREAMS}/__mock/calls`, {
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
	await seed(page, {
		'GET /v1/model': { models: [DEMO, FRAUD] },
		'GET /v1/model/demo': describeOf(DEMO, DEMO_ARTIFACTS),
		'GET /v1/model/fraud': describeOf(FRAUD, []),
		// Default: the metrics store is up and has NOTHING for these models. Every detail-panel test
		// expands a row, so without this each one would render "metrics store unreachable" — a state that
		// is not what it is testing.
		[CURVE_RANGE]: promRange([]),
	});
});

test('registry list renders candidate/blessed state per model', async ({ page }) => {
	await page.goto('/models');
	await expect(page.getByRole('heading', { name: 'Model registry' })).toBeVisible();
	const demoRow = page.locator('tr', { hasText: 'demo' }).first();
	await expect(demoRow).toContainText('v3');
	await expect(demoRow).toContainText('blessed behind');
	const fraudRow = page.locator('tr', { hasText: 'fraud' }).first();
	await expect(fraudRow).toContainText('candidate only');
});

test('clicking a model opens the candidate-vs-blessed metrics panel', async ({ page }) => {
	await page.goto('/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	await expect(page.locator('.metrics')).toBeVisible();
	await expect(page.locator('.metrics')).toContainText('rows_seen');
	await expect(page.locator('.metrics')).toContainText('0.1234'); // candidate loss
	await expect(page.locator('.metrics')).toContainText('0.5000'); // blessed loss
});

test('the detail lists the artifacts as a sortable table', async ({ page }) => {
	await page.goto('/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	const artifacts = page.getByLabel('Artifacts for demo');
	await expect(artifacts).toContainText('3/weights.json');
	await expect(artifacts).toContainText('2.0 KiB'); // size_bytes rendered human-readable
	await expect(artifacts).toContainText('3/scaler.json');
	await expect(artifacts).toContainText('—'); // null updated_at renders honestly
});

test('an artifact-less model shows the honest empty artifacts state', async ({ page }) => {
	await page.goto('/models');
	await page.locator('td', { hasText: 'fraud' }).first().click();
	await expect(page.getByLabel('Artifacts for fraud')).toContainText('No artifacts listed');
});

test('training curves plot where series exist and state the truth where none do', async ({
	page,
}) => {
	// demo reads while the range query answers with a real matrix → its curves plot for real, from the
	// flattened [unix, value] pairs `promqlRange` produces (the seeded body is the raw Prometheus answer,
	// so the query URL, the parse and the LayerChart render are all exercised).
	await seed(page, {
		[CURVE_RANGE]: promRange([
			[1753351200, 4],
			[1753354800, 9],
		]),
	});
	await page.goto('/models');
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
	// The old assertion here ("Curve Cumulative training runs" absent WHILE the rows curve plots) cannot
	// be expressed and is deliberately not faked: the curve reads are server-side PromQL, and the mock can
	// only key a `query_range` seed on the path — its `start`/`end` come from the server clock — so all
	// three curve queries necessarily share one answer. The point-less-curve filter is pinned below at the
	// all-empty granularity instead; the mixed case needs the mock to match seeds on the `query` parameter.
	await seed(page, { [CURVE_RANGE]: promRange([]) });
	await page.locator('td', { hasText: 'demo' }).first().click(); // collapse
	await page.locator('td', { hasText: 'fraud' }).first().click();
	await expect(page.getByText('No training series recorded for this model')).toBeVisible();
	await expect(page.getByLabel(/^Curve /)).toHaveCount(0);
});

test('bless promotes the candidate and the row updates', async ({ page }) => {
	await page.goto('/models');
	const demoRow = page.locator('tr', { hasText: 'demo' }).first();
	const bless = demoRow.getByRole('button', { name: 'bless v3' });
	await expect(bless).toBeVisible();
	// The catalog's world AFTER the promote. Seeded once the pre-promote list is on screen, so the
	// single-flight re-read the command fires lands on the moved pointer — the mock is a fixed response
	// per key, so "what changed" has to be stated rather than simulated.
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

test('the sidebar marks Registry active, and the navbar panels soft-nav in-zone / hard-nav out', async ({
	page,
}) => {
	await page.goto('/models');
	// Registry IS this zone's root, matched with `exact` — lit here and not on a sibling sub-route.
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Registry' })).toBeVisible();

	// THE INVERSION THIS MOVE CAUSED. While these routes lived at `/lakehouse/models`, Lineage was an area
	// of the SAME zone and its panel rows had to soft-navigate; the assertion was that they carried NO
	// reload marker. From the models zone every lakehouse row leaves this app's route manifest, so the
	// marker is now mandatory — without it SvelteKit soft-navigates into a route this zone does not own
	// and 404s. Same contract, opposite sign, asserted from both sides below.
	const lakehouse = await openPanel(page, 'Lakehouse');
	for (const href of ['/lakehouse/lineage', '/lakehouse/catalog/tables']) {
		await expect(lakehouse.locator(`a[href="${href}"]`)).toHaveCount(1);
		await expect(lakehouse.locator(`a[href="${href}"]`)).toHaveAttribute(
			'data-sveltekit-reload',
			'',
		);
	}

	// …and this zone's OWN panel rows stay soft navigations.
	await closePanel(page);
	const models = await openPanel(page, 'Models');
	for (const href of ['/models/', '/models/experiments', '/models/runs']) {
		await expect(models.locator(`a[href="${href}"]`)).not.toHaveAttribute(
			'data-sveltekit-reload',
			'',
		);
	}
});
