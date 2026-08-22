import { test, expect, type Page } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { MOCK_UPSTREAMS } from './ports';

// Hermetic /models/experiments coverage (#53). Moved here from `lakehouse/e2e/admin/models-experiments.spec.ts`
// with the route itself: the PromQL runs FOR REAL on the zone server (`experiments.remote.ts`) against raw
// Prometheus payloads seeded per bearer on the mock upstream — these tests exercise the actual query URLs
// and series flattening instead of a pre-shaped dashboard stub.
//
// Two page.route-era states stay consciously RETIRED, not translated: the 401 sign-in state (the auth-ON
// harness's login-first gate redirects a signed-out navigation before the page can render an in-page 401)
// and the 501 no-observability state (GREPTIME_API is always set on this server — the branch exists for
// un-provisioned deployments the hermetic harness deliberately does not model). Both renderings live in
// the component; the pipeline states that remain reachable are covered.

const PANEL_QUERIES = {
	runs: 'sum by (lance_model) (rate(lance_training_runs_total[5m]))',
	rows: 'max by (lance_model) (lance_training_rows_seen)',
	features: 'max by (lance_model) (lance_training_features)',
} as const;

const promURL = (q: string) => `GET /v1/prometheus/api/v1/query?query=${encodeURIComponent(q)}`;

/** A raw Prometheus instant-query body with one value per model. */
const series = (values: Record<string, number>) => ({
	status: 'success',
	data: {
		result: Object.entries(values).map(([model, value]) => ({
			metric: { lance_model: model },
			value: [1753280000, String(value)] as [number, string],
		})),
	},
});

let token: string;

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

const seed = (page: Page, routes: Record<string, unknown>) =>
	page.request.post(`${MOCK_UPSTREAMS}/__mock/seed`, { data: { bearer: token, routes } });

test('renders the embedded training dashboard from real PromQL answers', async ({ page }) => {
	await seed(page, {
		[promURL(PANEL_QUERIES.runs)]: series({ demo: 0.02 }),
		[promURL(PANEL_QUERIES.rows)]: series({ demo: 8, fraud: 12 }),
		[promURL(PANEL_QUERIES.features)]: series({ demo: 1 }),
	});
	await page.goto('/models/experiments');
	await expect(page.getByRole('heading', { name: 'Experiments' })).toBeVisible();
	await expect(page.locator('.src')).toContainText('Model Training');
	await expect(page.locator('.src')).toContainText('GreptimeDB');
	// The three panels and a per-model bar row each render.
	await expect(page.locator('section h2')).toHaveCount(3);
	await expect(page.locator('.bars li .model', { hasText: 'fraud' })).toBeVisible();
	await expect(page.locator('.bars li .val', { hasText: '12' })).toBeVisible();
	// No credential ever reaches the browser (the PromQL runs server-side).
	await expect(page.locator('.page')).not.toContainText('password');
});

test('shows the offline state when the metrics store is unreachable', async ({ page }) => {
	// A 500 from Greptime makes every panel query throw → the honest 502 offline state.
	await seed(page, {
		[promURL(PANEL_QUERIES.runs)]: { status: 500, body: {} },
		[promURL(PANEL_QUERIES.rows)]: { status: 500, body: {} },
		[promURL(PANEL_QUERIES.features)]: { status: 500, body: {} },
	});
	await page.goto('/models/experiments');
	await expect(page.locator('.empty')).toContainText('unreachable');
});
