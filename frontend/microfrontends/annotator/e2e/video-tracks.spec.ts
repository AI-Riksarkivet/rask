import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MOCK_ANNOTATOR } from './ports';

// Object tracks on the VIDEO canvas — tracks.ts consumed for real, in a real browser.
//
// The unit suite pins the maths (interpolation, lifetime, keyframe promotion). What only a browser
// can prove is the display half: that seeking the clip MOVES the interpolated box on the canvas,
// that outside the track's lifetime the object is ABSENT (never clamped to a stale keyframe), and
// that "+ keyframe" turns a selection into a longer track without touching the save payload's
// meaning. Pixel comparison, not DOM: the canvas is the only surface the overlay changes — the
// same reasoning as delete-visible.spec.ts.
//
// The clip is a 5 s flat-colour VP8 fixture (e2e/fixtures/clip.webm, 2 KB) so the ONLY thing that
// varies between screenshots is the annotation overlay.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const CLIP = readFileSync(join(import.meta.dirname, 'fixtures', 'clip.webm'));

/** One track: two keyframes at t=0 and t=4 — far apart, so interpolation is unmistakable. */
function annotationsIPC(): Buffer {
	const table = tableFromArrays({
		id: ['kf-a', 'kf-b'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([10, 200]),
		y: Float32Array.from([20, 120]),
		width: Float32Array.from([80, 100]),
		height: Float32Array.from([60, 80]),
		t_start: Float32Array.from([0, 4]),
		t_end: Float32Array.from([0, 4]),
		group: ['trk-rider', 'trk-rider'],
		label: ['rider', 'rider'],
		status: ['accepted', 'accepted'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
	// Range-aware on purpose: Chromium marks a plain-200 media response unseekable
	// (`video.seekable` stays empty), and every `currentTime` assignment silently no-ops —
	// which presents as "seeking does nothing" three layers up.
	await page.route('**/annotator/clip.webm', (route) => {
		const range = route.request().headers()['range'];
		if (range) {
			const m = /bytes=(\d+)-(\d*)/.exec(range);
			const start = Number(m?.[1] ?? 0);
			const end = m?.[2] ? Number(m[2]) : CLIP.length - 1;
			return route.fulfill({
				status: 206,
				headers: {
					'content-type': 'video/webm',
					'accept-ranges': 'bytes',
					'content-range': `bytes ${start}-${end}/${CLIP.length}`,
				},
				body: CLIP.subarray(start, end + 1),
			});
		}
		return route.fulfill({
			status: 200,
			headers: { 'content-type': 'video/webm', 'accept-ranges': 'bytes' },
			body: CLIP,
		});
	});
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

async function openClip(page: import('@playwright/test').Page) {
	await page.goto(
		`/annotator/?keys=${encodeURIComponent(KEY)}&kind=video&media=${encodeURIComponent('/annotator/clip.webm')}`,
	);
	// Both keyframes reached the sidebar — the engine has the rows.
	await expect(page.getByTestId('annotation-list')).toContainText('rider');
	await expect(page.getByTestId('add-keyframe')).toBeVisible();
	// The clip DECODED — duration is on the transport. Without this gate a broken media route
	// makes every seek a silent no-op and the interpolation assertions fail for the wrong reason.
	await expect(page.getByText('/ 0:05')).toBeVisible({ timeout: 10_000 });
	// Let the first frame snapshot + track sync land.
	await page.waitForTimeout(2000);
}

test('seeking MOVES the interpolated box; outside the lifetime the object is absent', async ({
	page,
}) => {
	// Assertions ride HIT-TESTING, not pixels — the same reasoning as delete-visible.spec.ts:
	// a WebGL canvas screenshot is unreliable (the drawing buffer is not preserved after
	// present), while "click where the box should be and see whether the canvas offers it"
	// reads the exact overlay state the track sync writes.
	await openClip(page);
	const canvas = page.locator('canvas').first();
	await expect(canvas).toBeVisible();

	// The clip is 320x240, fit-centred (ImagePlugin.fitToViewport) — map image → client coords.
	const bb = (await canvas.boundingBox())!;
	const s = Math.min(bb.width / 320, bb.height / 240);
	const at = (ix: number, iy: number) => ({
		x: (bb.width - 320 * s) / 2 + ix * s,
		y: (bb.height - 240 * s) / 2 + iy * s,
	});
	const detail = page.getByTestId('annotation-detail');

	// t=0 — the box sits AT the first keyframe (10,20 80x60; centre 50,50).
	await canvas.click({ position: at(50, 50), force: true });
	await expect(detail, 'the canvas did not offer the box at its own keyframe').toBeVisible();
	await expect(detail).toContainText('rider');
	await page.keyboard.press('Escape');
	await expect(detail).not.toBeVisible();

	// Seek to t=2.0, halfway between the keyframes — by driving the <video> element directly.
	// The app's own `seeked`/`timeupdate` handlers do everything under test (frame snapshot,
	// time cursor, track sync); only the GESTURE is synthetic, chosen over slider/keyboard for
	// determinism (a track click is proportional, a frame step rides an fps estimate).
	await page.evaluate(() => {
		document.querySelector('video')!.currentTime = 2;
	});
	await expect(page.getByText(/0:02\.0 \/ 0:05/)).toBeVisible();
	await page.waitForTimeout(800);

	// The keyframe's old position is EMPTY now…
	await canvas.click({ position: at(50, 50), force: true });
	await expect(detail, 'the box still hit-tests at t=0’s position after seeking').not.toBeVisible();
	// …and the interpolated position offers the object (t=2: 105,70 90x70; centre 150,105).
	await canvas.click({ position: at(150, 105), force: true });
	await expect(detail, 'the interpolated box is not where interpolation puts it').toBeVisible();
	await expect(detail).toContainText('rider');
	await page.keyboard.press('Escape');

	// Past the last keyframe (t=4.6 of the 5 s clip; the track ends at 4 s): the object is
	// ABSENT everywhere — a clamped box here would invent geometry nobody annotated.
	await page.evaluate(() => {
		document.querySelector('video')!.currentTime = 4.6;
	});
	await expect(page.getByText(/0:04\.6 \/ 0:05/)).toBeVisible();
	await page.waitForTimeout(800);
	for (const [ix, iy] of [
		[50, 50],
		[150, 105],
		[250, 160], // the LAST keyframe's centre — absence, not clamping
	] as const) {
		await canvas.click({ position: at(ix, iy), force: true });
		await expect(detail, `the track survived past its lifetime at (${ix},${iy})`).not.toBeVisible();
	}
});

test('+ keyframe extends the track from the current selection at the current moment', async ({
	page,
}) => {
	await openClip(page);

	// Nothing selected ⇒ the control says why it cannot act.
	const addKf = page.getByTestId('add-keyframe');
	await expect(addKf).toBeDisabled();

	// Select a keyframe row in the sidebar, then add — one more 'rider' row appears, pending save.
	// NOT force-clicked: the floating pills used to overhang the transport bar and eat these
	// clicks (Playwright's interception check found it); the shell now raises them above the bar
	// on temporal units, so an honest click doubles as the regression test for that overlap.
	await page.getByTestId('annotation-list').getByRole('button').first().click();
	await expect(addKf).toBeEnabled();
	await addKf.click();

	// Selecting swapped the list for the detail pane; Escape clears the selection so the LIST is
	// what gets counted — three 'rider' rows now, the third pending save.
	await page.keyboard.press('Escape');
	await expect(
		page.getByTestId('annotation-list').getByRole('button').filter({ hasText: 'rider' }),
	).toHaveCount(3);
});
