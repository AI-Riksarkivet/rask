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

function ipc(
	rows: { shape: string; label: string; status: string; text: string; reviewer?: string }[],
): Buffer {
	const table = tableFromArrays({
		id: rows.map((_, i) => `r${i}`),
		shape_type: rows.map((r) => r.shape),
		label: rows.map((r) => r.label),
		status: rows.map((r) => r.status),
		text: rows.map((r) => r.text),
		reviewer: rows.map((r) => r.reviewer ?? ''),
		updated_at: rows.map((r, i) => (r.reviewer ? 1000 + i : 0)),
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

	// One row per item, titled from the project. NO queue chrome: items are already claimed
	// into the bulk session, so claim state and assignee have no column here.
	await expect(page.getByTestId('bulk-title')).toContainText('Court records');
	const rows = page.getByTestId('bulk-row');
	await expect(rows).toHaveCount(2);
	await expect(rows.first()).toContainText('doc1/0/1');
	await expect(rows.first()).not.toContainText('claimed');
	await expect(rows.first()).not.toContainText('gina');

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

	// The AUTOFILTER: a content filter narrows rows to matches (both summaries are read here,
	// so the eager load is a no-op); clearing it restores the set.
	await page.getByTestId('bulk-filter-text').fill('Anno');
	await expect(rows).toHaveCount(1);
	await expect(rows.first()).toContainText('doc1/0/1');
	await page.getByTestId('bulk-filter-text').fill('nothing-matches-this');
	await expect(page.getByTestId('bulk-no-match')).toBeVisible();
	await page.getByTestId('bulk-filter-text').fill('');
	await expect(rows).toHaveCount(2);
});

test('accept and inline edit ride the save wire — OCC version echoed, attribution rendered back', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /projects/p1': { project: PROJECT, legal_events: [] },
				'GET /projects/p1/tasks': {
					tasks: { t1: 'claimed' },
					counts: { claimed: 1 },
					total: 1,
					terminal: 0,
					may_publish: false,
					details: [taskItem('t1', 'doc1/0/1', 'claimed', 'gina')],
				},
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);

	// A STATEFUL wire: each save advances the table version and the served rows, the way the
	// real backend's merge-commit does — so the test proves the grid re-fetches and re-renders
	// what the SERVER stamped (reviewer), never what the client claimed.
	const saves: Array<{ edits: Array<Record<string, string>>; base_version: number | null }> = [];
	let phase: 'initial' | 'accepted' | 'edited' = 'initial';
	const served = () => {
		if (phase === 'initial')
			return {
				version: '1',
				body: ipc([
					{ shape: 'bbox', label: 'paragraph', status: 'accepted', text: 'Anno 1632' },
					{ shape: 'bbox', label: 'stamp', status: 'prediction', text: '' },
				]),
			};
		const text = phase === 'edited' ? 'Anno 1632 den 14 maij' : 'Anno 1632';
		return {
			version: phase === 'accepted' ? '2' : '3',
			body: ipc([
				{ shape: 'bbox', label: 'paragraph', status: 'accepted', text, reviewer: 'gina@dev' },
				{ shape: 'bbox', label: 'stamp', status: 'accepted', text: '', reviewer: 'gina@dev' },
			]),
		};
	};
	await page.route('**/annotator/api/annotations/**', async (route) => {
		if (route.request().method() === 'POST') {
			const body = route.request().postDataJSON();
			saves.push({ edits: body.edits, base_version: body.base_version });
			phase = body.edits.some((e: Record<string, string>) => e.status) ? 'accepted' : 'edited';
			return json(route, { version: saves.length + 1, touched: body.edits.length });
		}
		const { version, body } = served();
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': version },
			body,
		});
	});

	await page.goto('/annotator/bulk?task=p1');
	const row = page.getByTestId('bulk-row').first();

	// ✓ ACCEPT: one save flips every prediction row, echoing the version the row rendered from.
	await row.getByTestId('bulk-accept').click();
	await expect(row.getByTestId('bulk-reviewed')).toContainText('gina@dev');
	expect(saves[0]).toEqual({ edits: [{ id: 'r1', status: 'accepted' }], base_version: 1 });
	await expect(row.getByTestId('bulk-regions')).not.toContainText('prediction');
	await expect(row.getByTestId('bulk-accept')).toHaveCount(0);

	// ✎ EDIT: the excerpt's own row is patched, against the version the accept advanced to.
	await row.getByTestId('bulk-text-edit').click();
	const editor = row.getByTestId('bulk-text-editor');
	await expect(editor).toHaveValue('Anno 1632');
	await editor.fill('Anno 1632 den 14 maij');
	await row.getByTestId('bulk-text-save').click();
	await expect(row.getByTestId('bulk-text')).toContainText('Anno 1632 den 14 maij');
	expect(saves[1]).toEqual({
		edits: [{ id: 'r0', text: 'Anno 1632 den 14 maij' }],
		base_version: 2,
	});
	await expect(row.getByTestId('bulk-text-editor')).toHaveCount(0);
});

test('act-first ＋column: one action derives the declaration, PATCHes the ontology, fills rows', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	// SIX items: the act-first add fills the first 5 (preview economics); the 6th stays empty
	// until the column-level ▶ apply runs the recipe over the remaining empty cells.
	const ITEMS = [1, 2, 3, 4, 5, 6].map((n) => `doc1/0/${n}`);
	const centuryOf = (key: string) => (key.endsWith('6') ? '18th' : '17th');
	const PATCHED_ONTOLOGY = {
		kind: '',
		modality: 'image',
		classes: [
			{
				name: 'century-document',
				tools: ['tag'],
				transcribe: true,
				attributes: [],
				required: false,
			},
		],
		relations: [],
		allow_empty: false,
	};
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /projects/p1': { project: PROJECT, legal_events: [] },
				'GET /projects/p1/tasks': {
					tasks: Object.fromEntries(ITEMS.map((_, i) => [`t${i + 1}`, 'claimed'])),
					counts: { claimed: ITEMS.length },
					total: ITEMS.length,
					terminal: 0,
					may_publish: false,
					details: ITEMS.map((key, i) => taskItem(`t${i + 1}`, key, 'claimed', 'gina')),
				},
				// The picker lists DISCOVERED producers only — the vlm family, returning `tag`.
				'GET /api/assist/producers': {
					producers: [
						{
							name: 'vlm',
							configured: false,
							returns: ['tag'],
							compatible: null,
							interactive: true,
						},
					],
					default_configured: false,
				},
				'PATCH /projects/p1/ontology': { ...PROJECT, ontology: PATCHED_ONTOLOGY },
				// The producer's answer per item: a `tag` shape whose text IS the cell value.
				...Object.fromEntries(
					ITEMS.map((key) => [
						`POST /api/assist/${key}`,
						{
							shapes: [
								{
									shape_type: 'tag',
									x: 0,
									y: 0,
									width: 0,
									height: 0,
									text: centuryOf(key),
									confidence: 0.8,
								},
							],
							source: 'model:vlm',
							dropped: [],
						},
					]),
				),
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	// Stateful annotations wire: after a save with inserts, the item serves its recipe row back.
	const saves: Array<{ path: string; inserts: Array<Record<string, unknown>> }> = [];
	const filled = new Set<string>();
	await page.route('**/annotator/api/annotations/**', async (route) => {
		const path = new URL(route.request().url()).pathname;
		const item = ITEMS.find((key) => path.includes(key)) ?? ITEMS[0];
		if (route.request().method() === 'POST') {
			const body = route.request().postDataJSON();
			if (body.inserts?.length) {
				saves.push({ path, inserts: body.inserts });
				filled.add(item);
			}
			return json(route, { version: 2, touched: 1 });
		}
		const rows = filled.has(item)
			? ipc([
					{ shape: 'tag', label: 'century-document', status: 'prediction', text: centuryOf(item) },
				])
			: ipc([]);
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': filled.has(item) ? '2' : '1' },
			body: rows,
		});
	});

	await page.goto('/annotator/bulk?task=p1');

	// ONE textarea, one Enter: no name field, no type field — the declaration is derived.
	await page.getByTestId('bulk-add-column').click();
	await page.getByTestId('bulk-column-action').fill('which century is this document from?');
	await expect(page.getByTestId('bulk-column-producer')).toHaveValue('vlm');
	await page.getByTestId('bulk-column-go').click();

	// The column exists immediately (named from the action) and the PREVIEW rows fill — the
	// first 5, not all 6: judge the prompt cheaply before running everything.
	await expect(page.getByTestId('bulk-column-century-document')).toBeVisible();
	const rows = page.getByTestId('bulk-row');
	await expect(rows.first().getByTestId('bulk-cell-century-document')).toContainText('17th');
	await expect(rows.nth(4).getByTestId('bulk-cell-century-document')).toContainText('17th');
	await expect(rows.nth(5).getByTestId('bulk-cell-century-document')).toContainText('—');

	// The column-level APPLY (▶): the recipe runs over the remaining EMPTY cells only.
	await page.getByTestId('bulk-fill-century-document').click();
	await expect(rows.nth(5).getByTestId('bulk-cell-century-document')).toContainText('18th');
	// 6 fills total — filled cells were skipped, not re-run.
	expect(saves).toHaveLength(6);

	// The silent PATCH carried the DERIVED declaration: tag-tooled, transcribing.
	const calls = await (await page.request.get(`${MOCK_ANNOTATOR}/__mock/calls`)).json();
	const patch = calls.calls.find(
		(c: { method: string; path: string }) =>
			c.method === 'PATCH' && c.path === '/projects/p1/ontology',
	);
	expect(patch.body.ontology.classes).toEqual([
		expect.objectContaining({ name: 'century-document', tools: ['tag'], transcribe: true }),
	]);

	// Each cell landed as a tag row through the ordinary save wire, provenance stamped.
	expect(saves[0].inserts[0]).toMatchObject({
		shape_type: 'tag',
		label: 'century-document',
		text: '17th',
		status: 'prediction',
		source: 'model:vlm',
	});

	// Per-cell accept: the same status flip, scoped to one cell.
	const acceptSaves: Array<Record<string, unknown>> = [];
	await page.route('**/annotator/api/annotations/**', async (route) => {
		if (route.request().method() === 'POST') {
			acceptSaves.push(route.request().postDataJSON());
			return json(route, { version: 3, touched: 1 });
		}
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '3' },
			body: ipc([{ shape: 'tag', label: 'century-document', status: 'accepted', text: '17th' }]),
		});
	});
	await rows.first().getByTestId('bulk-cell-accept').click();
	await expect(rows.first().getByTestId('bulk-cell-accept')).toHaveCount(0);
	expect(acceptSaves[0].edits).toEqual([{ id: 'r0', status: 'accepted' }]);
});

test('the Bulk TAB + the embedding view: anchor a row, the neighborhood orders and narrows the grid', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	const KEYS = ['doc1/0/1', 'doc1/0/2', 'doc1/0/3'];
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /projects/p1': { project: PROJECT, legal_events: [] },
				'GET /projects/p1/tasks': {
					tasks: Object.fromEntries(KEYS.map((_, i) => [`t${i + 1}`, 'claimed'])),
					counts: { claimed: KEYS.length },
					total: KEYS.length,
					terminal: 0,
					may_publish: false,
					details: KEYS.map((key, i) => taskItem(`t${i + 1}`, key, 'claimed', 'gina')),
				},
				// The by-key door answers the DECLARED identity — what associates hits onto rows.
				'POST /api/atlas/chunks/by-key': {
					rows: [],
					key_fields: ['doc_id', 'speech_id', 'chunk_id'],
				},
				// The estate's ONE similarity seam: nearest itself, then /3 close, then /2 far.
				'GET /api/search/similar': [
					{ doc_id: 'doc1', speech_id: 0, chunk_id: 1, _distance: 0 },
					{ doc_id: 'doc1', speech_id: 0, chunk_id: 3, _distance: 0.2 },
					{ doc_id: 'doc1', speech_id: 0, chunk_id: 2, _distance: 0.9 },
				],
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	await page.route('**/annotator/api/annotations/**', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: ipc([]),
		}),
	);

	// Bulk is a MODE of the labeling task — a tab beside Labeling, not a separate destination.
	await page.goto('/annotator/tasks/p1');
	await page.getByTestId('tab-bulk').click();
	const rows = page.getByTestId('bulk-row');
	await expect(rows).toHaveCount(3);
	await expect(rows.nth(1)).toContainText('doc1/0/2');

	// Anchor the first row: the grid re-orders nearest-first and shows each row's distance.
	await rows.first().getByTestId('bulk-similar').click();
	await expect(page.getByTestId('bulk-similarity-bar')).toContainText('similar to doc1/0/1');
	await expect(rows.nth(1)).toContainText('doc1/0/3'); // 0.2 before 0.9
	await expect(rows.nth(1).getByTestId('bulk-distance')).toContainText('0.200');
	await expect(rows.nth(2)).toContainText('doc1/0/2');
	await expect(page.getByTestId('bulk-similarity-count')).toContainText('3 of 3');

	// Tightening the cutoff narrows the working set — the rows every set action operates on.
	await page.getByTestId('bulk-similarity-cutoff').fill('0.5');
	await expect(rows).toHaveCount(2);
	await expect(page.getByTestId('bulk-similarity-count')).toContainText('2 of 3');

	// Clearing restores the whole session.
	await page.getByTestId('bulk-similarity-clear').click();
	await expect(rows).toHaveCount(3);
	await expect(rows.nth(1)).toContainText('doc1/0/2');
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
