import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// The TEXT CANVAS — a `text` unit renders the document AS the central surface (doccano/Argilla),
// with no pixel medium anywhere: no Pixi canvas, no image backdrop, no drawing tools. The document
// text fills the workspace at reading size, the class legend doubles as the keymap, and labeling
// is select-then-digit. This spec pins the three things that make it a TEXT tool rather than an
// image tool with a text panel: the absence of the pixel canvas, the document as the surface, and
// the keyboard gesture.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;

const L1 = 'Gustaf Eriksson Vasa reste till Dalarna år 1520';
const L2 = 'och samlade dalkarlarna mot Kristian Tyrann';

function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['l1', 'l2', 's1'],
		shape_type: ['text', 'text', 'text'],
		x: Float32Array.from([0, 0, 0]),
		y: Float32Array.from([10, 20, 0]),
		width: Float32Array.from([0, 0, 0]),
		height: Float32Array.from([0, 0, 0]),
		text: [L1, L2, ''],
		parent_id: ['', '', 'l1'],
		char_start: Int32Array.from([-1, -1, 0]),
		char_end: Int32Array.from([-1, -1, 20]),
		label: ['', '', 'person'],
		status: ['accepted', 'accepted', 'accepted'],
		source: ['ingest', 'ingest', 'human'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

const TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: 'text' },
	ontology: {
		kind: 'token-classification',
		modality: 'text',
		classes: [
			{ name: 'person', colour: null, tools: ['text'], attributes: [], required: false },
			{ name: 'place', colour: null, tools: ['text'], attributes: [], required: false },
		],
		relations: [],
		allow_empty: false,
	},
};

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: { routes: { 'GET /tasks/t1': TASK } },
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
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
	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&kind=text&task=t1`);
	await expect(page.getByTestId('text-canvas')).toBeVisible();
});

test('a text unit has NO pixel canvas — the document is the surface', async ({ page }) => {
	// The complaint this viewer answers: text labeling rendered inside an image-annotation view.
	// A text unit must mount no Pixi <canvas>, no image backdrop, and no drawing tools.
	await expect(page.locator('canvas')).toHaveCount(0);
	await expect(page.getByTitle(/^Brush/)).toHaveCount(0);
	await expect(page.getByTitle(/^Rectangle/)).toHaveCount(0);

	// The document reads as a document: both lines, in order, with the existing span highlighted.
	const canvas = page.getByTestId('text-canvas');
	await expect(canvas).toContainText(L2);
	await expect(page.getByTestId('doc-surface-0').locator('mark')).toHaveText(
		'Gustaf Eriksson Vasa',
	);
	// The legend doubles as the keymap — the vocabulary on screen the whole session.
	await expect(page.getByTestId('text-canvas-legend')).toContainText('person');
	await expect(page.getByTestId('text-canvas-legend')).toContainText('place');
});

test('select · press a digit — the doccano gesture labels in place', async ({ page }) => {
	await page.evaluate(() => {
		const seg = [...document.querySelectorAll('[data-testid="doc-surface-0"] [data-start]')].find(
			(el) => el.textContent?.includes('Dalarna'),
		)!;
		const node = seg.firstChild!;
		const text = node.textContent!;
		const at = text.indexOf('Dalarna');
		const range = document.createRange();
		range.setStart(node, at);
		range.setEnd(node, at + 'Dalarna'.length);
		const sel = window.getSelection()!;
		sel.removeAllRanges();
		sel.addRange(range);
		window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
	});
	await expect(page.getByTestId('doc-surface-0').getByTestId('span-labeler')).toBeVisible();

	// `2` = the second class in the legend (place).
	await page.keyboard.press('2');

	const surface = page.getByTestId('doc-surface-0');
	await expect(surface.locator('mark').filter({ hasText: 'Dalarna' })).toBeVisible();
	await expect(surface.getByTestId('span-tag').filter({ hasText: 'place' })).toBeVisible();
});

test('picking a line is the same selection the queue and inspector share', async ({ page }) => {
	await page.getByTestId('text-canvas-pick').nth(1).click();

	await expect(page.getByTestId('annotation-detail')).toBeVisible();
	const input = page.getByTestId('annotation-detail').locator('input, textarea').first();
	await expect(input).toHaveValue(L2);
});
