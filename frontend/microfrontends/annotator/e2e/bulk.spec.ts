import { test, expect, type Route } from '@playwright/test';
import { tableFromArrays, tableToIPC } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// OPEN-BULK phase 1 — the labeling task as a read-only GRID. This spec pins the surface's three
// promises: the grid renders one row per ITEM with workflow state; each row's ANNOTATION state
// (status counts, item tags, transcription excerpt) is fetched lazily and rendered from the real
// Arrow wire; and a row links into the canvas with the full task/project context the exit needs.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const PROJECT = {
	project_id: 'p1',
	tenant: 'default',
	slug: 'court-records',
	title: 'Court records',
	description: '',
	state: 'labeling',
	review_required: true,
	lease_seconds: 1800,
	counts: {
		unassigned: 1,
		claimed: 1,
		in_review: 0,
		changes_requested: 0,
		accepted: 0,
		skipped: 0,
	},
	published: null,
	publish_error: null,
	publish_progress: null,
	pending_target_namespace: null,
};

const taskItem = (id: string, key: string, state: string, assignee: string | null) => ({
	task_id: id,
	project_id: 'p1',
	state,
	assignee,
	lease_expires_at: null,
	source: { kind: 'chunk', keys: [key], where: 'demo' },
	media: { kind: 'image' },
});

function ipc(rows: { shape: string; label: string; status: string; text: string }[]): Buffer {
	const table = tableFromArrays({
		id: rows.map((_, i) => `r${i}`),
		shape_type: rows.map((r) => r.shape),
		label: rows.map((r) => r.label),
		status: rows.map((r) => r.status),
		text: rows.map((r) => r.text),
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

test('the grid: one row per item, live annotation state per visible row, canvas links', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /projects/p1': { project: PROJECT, legal_events: [] },
				'GET /projects/p1/tasks': {
					tasks: { t1: 'claimed', t2: 'unassigned' },
					counts: { claimed: 1, unassigned: 1 },
					total: 2,
					terminal: 0,
					may_publish: false,
					details: [
						taskItem('t1', 'doc1/0/1', 'claimed', 'gina'),
						taskItem('t2', 'doc1/0/2', 'unassigned', null),
					],
				},
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	// Each item answers its OWN annotation table — the grid's state columns render from these.
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		const body = path.includes('doc1/0/1')
			? ipc([
					{ shape: 'bbox', label: 'paragraph', status: 'accepted', text: 'Anno 1632' },
					{ shape: 'bbox', label: 'stamp', status: 'prediction', text: '' },
					{ shape: 'tag', label: 'damaged', status: 'accepted', text: '' },
				])
			: ipc([]);
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body,
		});
	});

	await page.goto('/annotator/bulk?task=p1');

	// One row per item, titled from the project.
	await expect(page.getByTestId('bulk-title')).toContainText('Court records');
	const rows = page.getByTestId('bulk-row');
	await expect(rows).toHaveCount(2);
	await expect(rows.first()).toContainText('doc1/0/1');
	await expect(rows.first()).toContainText('claimed');
	await expect(rows.first()).toContainText('gina');

	// The LIVE annotation state, from the Arrow wire: counts by status, the item tag, the excerpt.
	const first = rows.first();
	await expect(first.getByTestId('bulk-regions')).toContainText('accepted');
	await expect(first.getByTestId('bulk-regions')).toContainText('2');
	await expect(first.getByTestId('bulk-regions')).toContainText('prediction');
	await expect(first.getByTestId('bulk-tags')).toContainText('damaged');
	await expect(first.getByTestId('bulk-text')).toContainText('Anno 1632');
	// An item with no annotations says so — never a spinner that outlives its fetch.
	await expect(rows.nth(1).getByTestId('bulk-regions')).toContainText('empty');

	// A row links into the CANVAS with the full context (keys + task + project + dataset).
	const href = await first.getByRole('link').first().getAttribute('href');
	expect(href).toContain('keys=doc1%2F0%2F1');
	expect(href).toContain('task=t1');
	expect(href).toContain('project=p1');
	expect(href).toContain('dataset=demo');
});

test('without a labeling task, the page says where to come from — no dead chrome', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/annotator/bulk');
	await expect(page.getByText('Bulk grid needs a labeling task.')).toBeVisible();
	await expect(page.getByTestId('bulk-grid')).toHaveCount(0);
});
