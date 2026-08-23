import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// The TEXT-AS-SURFACE span annotator (the doccano/Label-Studio NER interaction) — in a browser,
// because the whole point of the surface is browser behaviour: spans as inline highlights in
// flowing text, a DOM selection that becomes offsets, a label tag with its own remove control.
// The segmentation/colour maths is pinned DOM-free in span-render.test.ts; text-span.test.ts pins
// the controller rules (offset validation, parent-by-id). What only this spec can prove is that
// the three compose: render → select → label → tag, and tag → remove.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const LINE = 'Gustaf Eriksson Vasa reste till Dalarna år 1520';

/** An HTR line plus ONE existing span ("Gustaf Eriksson Vasa" → person) addressing it by id. */
function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['line-1', 'span-1'],
		shape_type: ['bbox', 'text'],
		x: Float32Array.from([40, 0]),
		y: Float32Array.from([60, 0]),
		width: Float32Array.from([700, 0]),
		height: Float32Array.from([80, 0]),
		text: [LINE, ''],
		parent_id: ['', 'line-1'],
		char_start: Int32Array.from([-1, 0]),
		char_end: Int32Array.from([-1, 20]),
		label: ['text line', 'person'],
		status: ['accepted', 'accepted'],
		source: ['model:htr', 'human'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

/** A token-classification task: the surface's label buttons come from ITS text classes. */
const TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: 'image' },
	ontology: {
		kind: 'token-classification',
		modality: 'image',
		classes: [
			{ name: 'text line', colour: null, tools: ['bbox'], attributes: [], required: false },
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
			headers: { 'X-Annotations-Version': '2' },
			body: annotationsIPC(),
		});
	});
});

async function openLine(page: import('@playwright/test').Page) {
	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&task=t1`);
	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('text line');
	await list.getByRole('button').first().click();
	await expect(page.getByTestId('span-surface')).toBeVisible();
}

test('existing spans render as highlights IN the text, tagged with their class', async ({
	page,
}) => {
	await openLine(page);
	const surface = page.getByTestId('span-surface');

	// The span's text is a <mark> — a highlight in the flowing text, not offsets beside a field.
	await expect(surface.locator('mark')).toHaveText('Gustaf Eriksson Vasa');
	await expect(surface.getByTestId('span-tag')).toContainText('person');
});

test('selecting text in the surface labels a span in place — one click per class', async ({
	page,
}) => {
	await openLine(page);
	const surface = page.getByTestId('span-surface');

	// Select "Dalarna" the way a person does — a real DOM selection over the rendered text (the
	// segment holding it starts at offset 20: "<U+200B> reste till Dalarna år 1520" — that
	// leading zero-width space is REAL text in the fixture, not a typo here. It used to be written
	// as the literal character, which made the comment self-demonstrating and invisible at once;
	// oxlint 1.79 flags a literal U+200B as irregular whitespace, so it is spelled out instead.
	await page.evaluate(() => {
		const seg = [...document.querySelectorAll('[data-testid="span-surface"] [data-start]')].find(
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
		seg.parentElement!.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
	});

	// The labeler follows the selection with one button per declared text class.
	const labeler = page.getByTestId('span-labeler');
	await expect(labeler).toContainText('Dalarna');
	await labeler.getByTestId('label-as-place').click();

	// The new span is now a highlight with its tag — and a queued row, pending save.
	await expect(surface.locator('mark').filter({ hasText: 'Dalarna' })).toBeVisible();
	await expect(surface.getByTestId('span-tag').filter({ hasText: 'place' })).toBeVisible();
});

test('the transcription is IN the workspace — a lane under the canvas, selection-synced', async ({
	page,
}) => {
	// The Label-Studio composition: page image on top, the document's text as a surface of the
	// SAME workspace under it — not a field in the side panel. The lane appears whenever rows
	// carry text (an HTR page), each line a full span surface in reading order.
	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&task=t1`);
	const lane = page.getByTestId('text-lane');
	await expect(lane).toBeVisible();
	await expect(lane).toContainText('Transcription · 1 line');

	// The line renders with its existing span as a highlight — in the lane, not just the pane.
	const laneSurface = page.getByTestId('lane-surface-0');
	await expect(laneSurface.locator('mark')).toHaveText('Gustaf Eriksson Vasa');

	// Picking the line here is the same selection the canvas and inspector share.
	await lane.getByTestId('text-lane-pick').first().click();
	await expect(page.getByTestId('annotation-detail')).toBeVisible();

	// And labeling happens IN the lane: select "Dalarna" on the lane surface, one click on a
	// class, and the new span is a highlight right there.
	await page.evaluate(() => {
		const seg = [...document.querySelectorAll('[data-testid="lane-surface-0"] [data-start]')].find(
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
		seg.parentElement!.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
	});
	await laneSurface.getByTestId('label-as-place').click();
	await expect(laneSurface.locator('mark').filter({ hasText: 'Dalarna' })).toBeVisible();

	// The lane collapses out of the way without leaving the workspace.
	await lane.getByTestId('text-lane-toggle').click();
	await expect(laneSurface).not.toBeVisible();
});

test('a tag’s ✕ removes its span without stealing the selection from the parent', async ({
	page,
}) => {
	await openLine(page);
	const surface = page.getByTestId('span-surface');

	await surface.getByRole('button', { name: /Remove person span/ }).click();

	await expect(surface.locator('mark')).toHaveCount(0);
	// Still on the parent line's detail — removing a child span must not close the pane.
	await expect(page.getByTestId('annotation-detail')).toBeVisible();
});
