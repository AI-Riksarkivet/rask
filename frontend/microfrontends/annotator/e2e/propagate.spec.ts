import { test, expect, type Page, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// FEW-SHOT PROPAGATION HAS A BUTTON. The seam (`/api/jobs/apply`, exemplar ids, scope levels,
// idempotent job ids) existed end to end and nothing called it. This spec pins the loop at the
// wire: pick an annotation, choose the scope, Propagate — ONE job posts with the exemplar's
// stable id and the unit-scoped selection, and the panel repeats the honest-mock answer instead
// of reading as success.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

function ipc(): Buffer {
	// MANY objects on the page — two of them will be picked as the few-shot exemplars.
	const table = tableFromArrays({
		id: ['ex-1', 'ex-2', 'other'],
		shape_type: ['bbox', 'bbox', 'bbox'],
		x: Float32Array.from([40, 240, 300]),
		y: Float32Array.from([40, 60, 200]),
		width: Float32Array.from([180, 160, 90]),
		height: Float32Array.from([120, 110, 60]),
		label: ['stamp', 'stamp', 'line'],
		status: ['accepted', 'accepted', 'accepted'],
		source: ['human', 'human', 'human'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

const calls = async (page: Page): Promise<{ method: string; path: string; body: unknown }[]> => {
	const res = await page.request.get(`${MOCK_ANNOTATOR}/__mock/calls`);
	return (await res.json()).calls as { method: string; path: string; body: unknown }[];
};

test('picking an exemplar and pressing Propagate posts ONE honest job', async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'POST /api/jobs/apply': { job_id: 'job-e2e', status: 'queued', backend: 'mock' },
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() !== 'GET') return json(route, { detail: 'unstubbed write' }, 404);
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: ipc(),
		});
	});

	// A MULTI-KEY ITEM: three units under one decision. Exemplars are labelled on page 1; the
	// pattern applies across all three — the INSID3 shape ("few normally-labelled objects become
	// the instruction"), where neither side is singular.
	const keys = [KEY, `${DOC}/0/20`, `${DOC}/0/21`];
	await page.goto(`/annotator/?keys=${encodeURIComponent(keys.join(','))}`);

	// No selection ⇒ no panel — a control that could submit "nothing" reads as broken.
	await expect(page.getByTestId('propagate-panel')).toHaveCount(0);

	// Pick TWO exemplars (Ctrl-click = the list's multi-select); the panel counts them.
	// BOTH picks are Ctrl-clicks: a plain click swaps the list for the inspector, and the second
	// exemplar would have nothing to be picked from.
	const list = page.getByTestId('annotation-list');
	await list
		.getByRole('button')
		.filter({ hasText: 'stamp' })
		.first()
		.click({ modifiers: ['Control'] });
	await list
		.getByRole('button')
		.filter({ hasText: 'stamp' })
		.nth(1)
		.click({ modifiers: ['Control'] });
	const panel = page.getByTestId('propagate-panel');
	await expect(panel).toBeVisible();
	await expect(panel.getByTestId('propagate-count')).toHaveText('2 exemplars');

	// The multi-item scope exists BECAUSE the item has 3 units, and says so.
	await panel.getByTestId('propagate-scope').selectOption('selection');
	await expect(panel.getByTestId('propagate-scope')).toContainText('all 3 items here');
	await panel.getByTestId('propagate-run').click();

	// The wire: ONE job — both exemplars by STABLE ID, all three unit keys in the scope.
	await expect
		.poll(
			async () => (await calls(page)).filter((c) => c.path.endsWith('/api/jobs/apply')).length,
			{ timeout: 10_000 },
		)
		.toBe(1);
	const job = (await calls(page)).find((c) => c.path.endsWith('/api/jobs/apply'))!;
	expect(job.body).toMatchObject({
		producer: 'insid3',
		op: 'propagate',
		exemplars: ['ex-1', 'ex-2'],
		scope: { level: 'chunks', keys },
	});

	// The WIRE answer, not the sync guess: job id + the honest mock warning, in the panel.
	await expect(panel.getByTestId('propagate-outcome')).toContainText('job-e2e');
	await expect(panel.getByTestId('propagate-outcome')).toContainText('MOCK');
});
