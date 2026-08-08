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
	const table = tableFromArrays({
		id: ['ex-1', 'other'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([40, 300]),
		y: Float32Array.from([40, 200]),
		width: Float32Array.from([180, 90]),
		height: Float32Array.from([120, 60]),
		label: ['stamp', 'line'],
		status: ['accepted', 'accepted'],
		source: ['human', 'human'],
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

	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}`);

	// No selection ⇒ no panel — a control that could submit "nothing" reads as broken.
	await expect(page.getByTestId('propagate-panel')).toHaveCount(0);

	// Pick the exemplar in the queue; the panel appears with the count.
	await page
		.getByTestId('annotation-list')
		.getByRole('button')
		.filter({ hasText: 'stamp' })
		.click();
	const panel = page.getByTestId('propagate-panel');
	await expect(panel).toBeVisible();
	await expect(panel.getByTestId('propagate-count')).toHaveText('1 exemplar');

	await panel.getByTestId('propagate-scope').selectOption('item');
	await panel.getByTestId('propagate-run').click();

	// The wire: one insid3 propagate job, exemplar addressed by STABLE ID, scope = this unit.
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
		exemplars: ['ex-1'],
		scope: { level: 'chunks', keys: [KEY] },
	});

	// The WIRE answer, not the sync guess: job id + the honest mock warning, in the panel.
	await expect(panel.getByTestId('propagate-outcome')).toContainText('job-e2e');
	await expect(panel.getByTestId('propagate-outcome')).toContainText('MOCK');
});
