import { test, expect, type Route } from '@playwright/test';

// Hermetic /models coverage: every catalog call the page makes goes through the /capi BFF proxy,
// stubbed here — no live catalog needed (same pattern as lineage.spec.ts).

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// Mutable so the promote test can flip the blessed pointer and assert the UI refresh.
let models: { model: string; latest_version: number | null; blessed_version: number | null }[];

test.beforeEach(async ({ page }) => {
	models = [
		{ model: 'demo', latest_version: 3, blessed_version: 2 },
		{ model: 'fraud', latest_version: 1, blessed_version: null },
	];
	// The detail's training curves come from the experiments BFF's ?model= mode: demo has a real
	// series, fraud has none (the honest empty state).
	await page.route('**/api/experiments**', (route) => {
		const url = new URL(route.request().url());
		const model = url.searchParams.get('model') ?? '';
		const points =
			model === 'demo'
				? [
						{ t: '2026-07-24T10:00:00Z', v: 4 },
						{ t: '2026-07-24T11:00:00Z', v: 9 },
					]
				: [];
		return json(route, {
			model,
			source: 'GreptimeDB (OTLP)',
			curves: [
				{ key: 'rows', title: 'Rows seen per run', points },
				{ key: 'features', title: 'Feature datasets per run', points: [] },
				{ key: 'runs', title: 'Cumulative training runs', points: [] },
			],
		});
	});
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		const promote = path.match(/^\/v1\/model\/([^/]+)\/promote$/);
		if (promote && req.method() === 'POST') {
			const name = decodeURIComponent(promote[1]);
			const body = req.postDataJSON() as { version: number };
			const entry = models.find((m) => m.model === name);
			if (entry) entry.blessed_version = body.version;
			return json(route, { model: name, blessed_version: body.version, tag: 'blessed' });
		}
		const one = path.match(/^\/v1\/model\/([^/]+)$/);
		if (one) {
			const name = decodeURIComponent(one[1]);
			const entry = models.find((m) => m.model === name);
			return json(route, {
				model: name,
				latest_version: entry?.latest_version ?? 1,
				blessed_version: entry?.blessed_version ?? null,
				candidate_metrics: { rows_seen: 9, loss: 0.1234 },
				blessed_metrics: entry?.blessed_version ? { rows_seen: 4, loss: 0.5 } : null,
				// The frozen contract's new field: the models/<model>/ object listing.
				artifacts:
					name === 'demo'
						? [
								{
									path: '3/weights.json',
									size_bytes: 2048,
									updated_at: '2026-07-24T09:00:00Z',
								},
								{ path: '3/scaler.json', size_bytes: 512, updated_at: null },
							]
						: [],
			});
		}
		if (path === '/v1/model') return json(route, { models });
		return json(route, { detail: 'unstubbed' }, 404);
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
	await page.goto('/lakehouse/models');
	await page.locator('td', { hasText: 'demo' }).first().click();
	// demo: the rows curve has points → one LayerChart plot; the empty curves are simply absent.
	await expect(page.getByLabel('Curve Rows seen per run')).toBeVisible();
	await expect(
		page.getByLabel('Curve Rows seen per run').locator('svg.lc-layout-svg'),
	).toBeVisible();
	await expect(page.getByLabel('Curve Cumulative training runs')).toHaveCount(0);
	// fraud: no series at all → the honest empty state, no fabricated flat line.
	await page.locator('td', { hasText: 'demo' }).first().click(); // collapse
	await page.locator('td', { hasText: 'fraud' }).first().click();
	await expect(page.getByText('No training series recorded for this model')).toBeVisible();
});

test('bless promotes the candidate and the row updates', async ({ page }) => {
	await page.goto('/lakehouse/models');
	const demoRow = page.locator('tr', { hasText: 'demo' }).first();
	await demoRow.getByRole('button', { name: 'bless v3' }).click();
	await expect(page.locator('.banner.ok')).toContainText('demo v3 is now blessed');
	await expect(demoRow).toContainText('blessed'); // state chip converges after the refetch
	await expect(demoRow.getByRole('button')).toHaveCount(0); // nothing left to bless
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
