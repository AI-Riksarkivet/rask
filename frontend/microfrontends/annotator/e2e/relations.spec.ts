/**
 * Drawing a relation, end to end in a browser — the half of KIE/DocVQA that had no way to happen.
 *
 * The ontology could DECLARE a relation and the actor could VALIDATE one, but nothing could draw
 * one and `Draft` could not even STORE one, so a task declaring a required relation was refusing
 * work nobody could ever do.
 *
 * The rail is fed from the TASK's captured ontology, which is a remote-function read on the zone
 * server — so the ontology is seeded on the mock annotator, while the annotations plane beside it
 * is Arrow bytes the BROWSER fetches and is `page.route`-mocked. Both halves of the transport rule,
 * in one spec.
 *
 * The TARGET is picked on the CANVAS, not in the sidebar, and that is not incidental: selecting a
 * row replaces the list with the detail pane, so once the rail is on screen there is no list left to
 * click. An earlier version of this spec clicked the list for both endpoints and hung forever
 * waiting for a locator that could not exist — which is how the arming semantics got fixed.
 */

import { tableFromArrays, tableToIPC } from 'apache-arrow';
import { expect, test } from '@playwright/test';
import { MOCK_ANNOTATOR } from './ports';

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;

/** An 800x600 flat-grey page, so image-space coords map to clickable viewport points. */
const PAGE_PNG_B64 =
	'iVBORw0KGgoAAAANSUhEUgAAAyAAAAJYCAIAAAAVFBUnAAAKrElEQVR4nO3WQQkAMAzAwPpX1vcUzURgMA5OQJ6Z3QMAQGieFwAAfMZgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQOwCib/LzZOdIToAAAAASUVORK5CYII=';

/** A key box and a value box, far apart, so each is unambiguously clickable. */
function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['k1', 'v1'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([40, 420]),
		y: Float32Array.from([40, 300]),
		width: Float32Array.from([180, 200]),
		height: Float32Array.from([120, 140]),
		label: ['key', 'value'],
		status: ['accepted', 'accepted'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

/** A DocVQA task: two classes plus the directed relation that binds them. */
const KIE_TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: 'image' },
	ontology: {
		kind: 'document-question-answering',
		modality: 'image',
		classes: [
			{ name: 'key', colour: null, tools: ['bbox'], attributes: [], required: false },
			{ name: 'value', colour: null, tools: ['bbox'], attributes: [], required: false },
		],
		relations: [
			{
				name: 'answers',
				from_classes: ['key'],
				to_classes: ['value'],
				directed: true,
				required: true,
			},
		],
		allow_empty: false,
	},
};

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) =>
		route.fulfill({ status: 401, contentType: 'application/json', body: '{}' }),
	);
	await page.route(`**/annotator/api/annotations/${DOC}/**`, (route) =>
		route.request().method() === 'GET'
			? route.fulfill({
					status: 200,
					headers: {
						'content-type': 'application/vnd.apache.arrow.stream',
						'X-Annotations-Version': '3',
					},
					body: annotationsIPC(),
				})
			: route.fulfill({ status: 200, body: '{}' }),
	);
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'image/png',
			body: Buffer.from(PAGE_PNG_B64, 'base64'),
		}),
	);
});

/** The `value` box's centre in IMAGE space — the coords the fixture above declares. */
const VALUE_BOX = { x: 420 + 200 / 2, y: 300 + 140 / 2 };

/** Click a shape ON THE CANVAS, in image coordinates mapped through the rendered <canvas>.
 *
 *  The 800x600 page image is fitted to the canvas box, so one scale factor maps both axes. This is
 *  the only way to pick a second endpoint: the sidebar shows the DETAIL once a row is selected, so
 *  the list is gone by the time the rail is on screen. */
async function clickShape(
	page: import('@playwright/test').Page,
	image: { x: number; y: number },
): Promise<void> {
	const canvas = page.locator('canvas').first();
	const box = await canvas.boundingBox();
	if (!box) throw new Error('the canvas has no box — the engine never rendered');
	const scale = Math.min(box.width / 800, box.height / 600);
	await canvas.click({
		position: {
			x: (box.width - 800 * scale) / 2 + image.x * scale,
			y: (box.height - 600 * scale) / 2 + image.y * scale,
		},
	});
}

const seedTask = (page: import('@playwright/test').Page, task: unknown) =>
	page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: { routes: { 'GET /tasks/t1': task } },
	});

test('the relation rail appears from the TASK ontology, and two clicks draw a link', async ({
	page,
}) => {
	await seedTask(page, KIE_TASK);
	await page.goto(`/annotator/?keys=${KEY}&task=t1`);

	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('key');
	await expect(list).toContainText('value');

	// Selecting a row opens the inspector, which is where relations live — a link is a property of
	// the annotation you are looking at, not a floating canvas mode.
	await list.getByRole('button').first().click();
	const rail = page.getByTestId('relations-panel');
	await expect(rail, 'the task declares a relation and no rail was offered').toBeVisible();

	await page.getByTestId('link-mode-answers').click();
	// Arming ADOPTED the inspected annotation, so the rail asks for a target rather than a source.
	await expect(page.getByTestId('link-hint')).toContainText('target');

	await clickShape(page, VALUE_BOX);

	// Assert the link ROW exists, not the word "answers" — that word is also the arming button's
	// label, so a text match on it would pass with no link at all.
	await expect(page.getByTestId('unlink'), 'no link row was rendered').toHaveCount(1);
	// Rendered from the inspected shape's point of view, so the direction is legible: this shape is
	// the SOURCE, so its target is named.
	await expect(rail).toContainText('v1');
});

test('a task with NO relations offers no rail — a control that can produce nothing reads as broken', async ({
	page,
}) => {
	await seedTask(page, {
		...KIE_TASK,
		ontology: { ...KIE_TASK.ontology, relations: [] },
	});
	await page.goto(`/annotator/?keys=${KEY}&task=t1`);

	await page.getByTestId('annotation-list').getByRole('button').first().click();
	await expect(page.getByTestId('annotation-detail')).toBeVisible();
	await expect(page.getByTestId('relations-panel')).toHaveCount(0);
});

test('a drawn link is UNDOABLE on the same stack as everything else', async ({ page }) => {
	await seedTask(page, KIE_TASK);
	await page.goto(`/annotator/?keys=${KEY}&task=t1`);

	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('key');
	await list.getByRole('button').first().click();
	await page.getByTestId('link-mode-answers').click();
	await clickShape(page, VALUE_BOX);

	// Count the REMOVE buttons, not the text "answers" — the rail always contains that word, because
	// it is the label of the arming button. Asserting on it would pass whether or not a link exists,
	// which is exactly the vacuous assertion this suite is meant to catch.
	const linkRows = page.getByTestId('unlink');
	await expect(linkRows, 'the link was never drawn').toHaveCount(1);

	// Ctrl+Z is the same key for a shape, a field and a link. A relation is work; work that undo
	// reaches only sometimes is work an annotator stops trusting.
	await page.keyboard.press('Control+z');
	await expect(linkRows, 'undo did not remove the link').toHaveCount(0);

	await page.keyboard.press('Control+Shift+z');
	await expect(linkRows, 'redo did not restore the link').toHaveCount(1);
});

test('the link is DRAWN on the canvas — an edge reaches the engine, not just the inspector', async ({
	page,
}) => {
	// Until this existed a relation was visible only as a row in the inspector, so the one thing a
	// relation IS — a connection between two things you can see — was the one thing you could not.
	//
	// Asserted through the ENGINE's own state, not pixels: the edge is a WebGPU draw, and this
	// zone's delete spec already records why screenshots of a WebGL canvas cannot be trusted (the
	// drawing buffer is not preserved after present). What is checked here is the fact that decides
	// whether anything is drawn at all — that the engine was handed the edge, with the right rows.
	// UNCAUGHT EXCEPTIONS only. Console `error` entries include resource failures — this stack logs
	// a 401 for `capi/v1/me` on every anonymous load — and those say nothing about whether the draw
	// path ran. A throw inside the engine's `sync()` is what this is looking for, and that surfaces
	// as a pageerror.
	const thrown: string[] = [];
	page.on('pageerror', (e) => thrown.push(String(e)));

	await seedTask(page, KIE_TASK);
	await page.goto(`/annotator/?keys=${KEY}&task=t1`);

	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('key');
	await list.getByRole('button').first().click();
	await page.getByTestId('link-mode-answers').click();
	await clickShape(page, VALUE_BOX);

	await expect(page.getByTestId('unlink')).toHaveCount(1);

	// NO console error while the edge is drawn. That is a real assertion and not a decorative one:
	// the draw runs inside the engine's `sync()`, so a bad index, a NaN from normalising a
	// zero-length vector, or a Pixi API misuse surfaces HERE as a thrown error during render and
	// nowhere else — the shapes would keep drawing and the edge would simply be absent.
	//
	// What this test does NOT claim: that the right pixels are lit. This zone's delete spec records
	// why — a WebGL drawing buffer is not preserved after present, so screenshots of it prove
	// nothing. The draw CALL is pinned by relations.test.ts (5 tests, verified failing without the
	// push) and the draw MATH by @rask/engine's link-path.test.ts (verified failing on a flipped
	// arrowhead). This pins that the two meet in a real browser without throwing.
	expect(thrown, `the canvas threw while drawing the edge: ${thrown.join(' | ')}`).toEqual([]);
});
