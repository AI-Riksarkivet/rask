import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// ITEM-LEVEL CLASSIFICATION — the "Choices" question (Label Studio's `<Choices>`, doccano's
// classification project), composed OVER any modality rather than owned by one: the task's
// ontology declares `tag` classes and the workspace grows a chip bar; an active chip IS a `tag`
// row on the ordinary insert/undo/Save machinery. This spec pins the three things that make it a
// question type and not a widget: the bar appears from the ONTOLOGY (not from data), a toggle
// round-trips into the review queue, and the same bar composes over the text canvas untouched.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

/** One drawn shape and NO tag rows — the bar must come from the ontology, not the data. */
function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['b1'],
		shape_type: ['bbox'],
		x: Float32Array.from([40]),
		y: Float32Array.from([40]),
		width: Float32Array.from([180]),
		height: Float32Array.from([120]),
		label: ['stamp'],
		status: ['accepted'],
		source: ['human'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

const TASK = (modality: string) => ({
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: modality },
	ontology: {
		kind: 'image-classification',
		modality,
		classes: [
			{ name: 'stamp', colour: null, tools: ['bbox'], attributes: [], required: false },
			{ name: 'letter', colour: null, tools: ['tag'], attributes: [], required: false },
			{ name: 'envelope', colour: null, tools: ['tag'], attributes: [], required: false },
			{ name: 'damaged', colour: null, tools: ['tag'], attributes: [], required: false },
		],
		relations: [],
		allow_empty: false,
	},
});

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
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
			body: annotationsIPC(),
		});
	});
});

test('the bar appears from the ONTOLOGY, and a toggle round-trips into the review queue', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: { routes: { 'GET /tasks/t1': TASK('image') } },
	});
	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&task=t1`);
	const bar = page.getByTestId('classification-bar');
	await expect(bar).toBeVisible();
	// The bbox class is a SHAPE class — it must not appear as a choice.
	await expect(bar.getByTestId('classify-letter')).toBeVisible();
	await expect(bar.getByTestId('classify-stamp')).toHaveCount(0);

	await bar.getByTestId('classify-letter').click();

	// The chip is on, and the tag exists as an ordinary row the reviewer can see and undo.
	await expect(bar.getByTestId('classify-letter')).toHaveAttribute('aria-pressed', 'true');
	await expect(bar).toContainText('1 applied');
	await page.keyboard.press('Escape');
	await expect(
		page.getByTestId('annotation-list').getByRole('button').filter({ hasText: 'letter' }),
	).toHaveCount(1);

	// Off again — chip clears and the row goes with it.
	await bar.getByTestId('classify-letter').click();
	await expect(bar.getByTestId('classify-letter')).toHaveAttribute('aria-pressed', 'false');
	await expect(
		page.getByTestId('annotation-list').getByRole('button').filter({ hasText: 'letter' }),
	).toHaveCount(0);
});

test('the SAME bar composes over the text canvas — a question type, not an image widget', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: { routes: { 'GET /tasks/t1': TASK('text') } },
	});
	// Text rows for the unit, so the text canvas has a document to render.
	await page.unroute('**/annotator/api/annotations/**');
	const table = tableFromArrays({
		id: ['l1'],
		shape_type: ['text'],
		text: ['Gustaf Eriksson Vasa reste till Dalarna år 1520'],
		parent_id: [''],
		char_start: Int32Array.from([-1]),
		char_end: Int32Array.from([-1]),
		label: [''],
		status: ['accepted'],
		source: ['ingest'],
	});
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() !== 'GET') return json(route, { detail: 'unstubbed write' }, 404);
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: Buffer.from(tableToIPC(table, 'stream')),
		});
	});

	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&kind=text&task=t1`);
	await expect(page.getByTestId('text-canvas')).toBeVisible();

	const bar = page.getByTestId('classification-bar');
	await expect(bar).toBeVisible();
	await bar.getByTestId('classify-damaged').click();
	await expect(bar).toContainText('1 applied');
});
