/**
 * A deleted annotation leaves the CANVAS immediately — not after Save + reload.
 *
 * The sidebar always dropped it; the canvas did not, because it renders from the immutable Arrow
 * table that is only replaced on reload. One delete, two answers.
 *
 * It asserts on HIT-TESTING, not pixels. The canvas is a `<canvas>`, so no DOM node's absence can
 * stand in for the shape, and a DOM assertion would only re-test the sidebar — the half that was
 * never broken. Pixels are the obvious alternative and do not work here: Playwright screenshots of
 * a WebGL canvas are unreliable because the drawing buffer is not preserved after present, so the
 * comparison passes or fails for reasons unrelated to the shape.
 *
 * Hit-testing is the right probe because it is the SAME `hiddenMask` the renderer consults. A row
 * the canvas still draws is a row the canvas will still select; one the overlay hides is neither
 * drawn nor clickable. So "click where the shape was and see whether the canvas offers it" reads
 * the exact state the fix changes.
 *
 * The rest of this zone's e2e mocks the annotations GET as a 404 (the documented failure path), so
 * its canvas holds zero shapes and could never have caught this. Here the route serves a real Arrow
 * IPC stream, which is also the only way the engine will draw anything.
 */

import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { expect, test } from '@playwright/test';

const DOC = 'fe00cd746463ad2c';

/** An 800x600 flat-grey page, so image-space shape coords map to clickable viewport points. */
const PAGE_PNG_B64 =
	'iVBORw0KGgoAAAANSUhEUgAAAyAAAAJYCAIAAAAVFBUnAAAKrElEQVR4nO3WQQkAMAzAwPpX1vcUzURgMA5OQJ6Z3QMAQGieFwAAfMZgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQMxgAQDEDBYAQOwCib/LzZOdIToAAAAASUVORK5CYII=';
const KEY = `${DOC}/0/19`;

/** Two boxes, far apart, so deleting one changes a large and unambiguous region of the canvas. */
function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['keep-me', 'delete-me'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([20, 300]),
		y: Float32Array.from([20, 300]),
		width: Float32Array.from([120, 160]),
		height: Float32Array.from([90, 120]),
		label: ['portrait', 'seal'],
		status: ['accepted', 'accepted'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

test('a deleted annotation disappears from the canvas without a save or reload', async ({
	page,
}) => {
	await page.route(`**/annotator/api/annotations/${DOC}/**`, (route) =>
		route.request().method() === 'GET'
			? route.fulfill({
					status: 200,
					headers: {
						'content-type': 'application/vnd.apache.arrow.stream',
						'X-Annotations-Version': '7',
					},
					body: annotationsIPC(),
				})
			: route.fulfill({ status: 200, body: '{}' }),
	);
	// A real-sized page image, because the shape coordinates are in IMAGE space: with a 1x1 png the
	// engine fits one pixel to the viewport and a shape at (300,300) lands nowhere clickable, which
	// makes the baseline click fail for a reason that has nothing to do with deletion.
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'image/png',
			body: Buffer.from(PAGE_PNG_B64, 'base64'),
		}),
	);

	await page.goto(`/annotator/?keys=${KEY}`);

	// Both rows reached the sidebar, so the engine has been handed two shapes to draw.
	const list = page.getByTestId('annotation-list');
	await expect(list).toContainText('portrait');
	await expect(list).toContainText('seal');

	// `force` on every canvas click: Playwright's actionability check never settles on a canvas the
	// engine repaints each frame, so it times out waiting for stability rather than failing on the
	// thing under test.
	const canvas = page.locator('canvas').first();
	await expect(canvas).toBeVisible();
	await page.waitForTimeout(1500);

	// Baseline: the canvas OFFERS the shape — clicking inside it opens the detail panel. Without
	// this the final assertion would pass on a canvas that never selected anything.
	const detail = page.getByTestId('annotation-detail');
	// Sampled with BOTH shapes drawn. Taken before any selection so a selection highlight cannot be
	// what differs later.
	const before = await canvas.screenshot();
	await canvas.click({ position: { x: 360, y: 350 }, force: true });
	await expect(detail, 'the canvas did not select the shape even BEFORE the delete').toBeVisible();
	await expect(detail).toContainText('seal');

	// The canvas click above already selected it, so delete straight away. (Clicking the row in the
	// list first is redundant and flaky — the detail panel opening shifts the list under the cursor.)
	await page
		.getByTitle(/Delete/i)
		.first()
		.click();
	await expect(list, 'the sidebar half regressed').not.toContainText('seal');

	// No save, no reload — the canvas must already be correct. Deselect first, so the comparison
	// cannot be satisfied by a selection highlight instead of the missing shape.
	await page.keyboard.press('Escape');
	await page.waitForTimeout(1200);

	// PIXELS, not the controller. Every controller-derived observable is contaminated: `selected`
	// resolves through `rows`, which already subtracts `_deletes`, so a detail panel that stops
	// showing the row proves only that the SIDEBAR half works — the half that was never broken. An
	// earlier version of this test asserted exactly that and passed with the fix reverted.
	//
	// The canvas bitmap is the one thing only the renderer controls.
	const after = await canvas.screenshot();
	expect(
		Buffer.compare(before, after),
		'the canvas is pixel-identical after a delete — the shape is still drawn. That is the ' +
			'defect: the sidebar drops the row while the canvas waits for a save + reload.',
	).not.toBe(0);

	// The neighbour must survive — a fix that blanked the canvas would also pass the check above.
	await canvas.click({ position: { x: 70, y: 60 }, force: true });
	await expect(detail, 'the WRONG shape was removed').toContainText('portrait');
});
