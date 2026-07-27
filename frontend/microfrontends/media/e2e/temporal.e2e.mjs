/**
 * Temporal (audio + video) annotator E2E — drives the REAL AudioViewer (wavesurfer
 * waveform, drag-to-create segments, region resize) and VideoViewer (frame snapshot
 * under the Pixi overlay, scrub, draw-on-frame pinned at the playhead) in a real
 * WebGPU Chromium, against fixture media in static/e2e/ (the demo doc has no media
 * blob). Same preconditions as the annotator suite (see lib.mjs / TESTING.md).
 */
// NOTE: `waitUntil: 'networkidle'` is deliberately absent from this file. Every zone's shell now holds a
// LIVE `query.live` stream open for the notification bell, so these apps have no idle network by design —
// the wait would sit until its timeout on every navigation. Each drive waits on the ELEMENT it is about
// to act on instead, which is the condition it actually needs.
import { tableFromIPC } from 'apache-arrow';
import { API, BASE, KEY, assertPreconditions, collector, launchBrowser, seed } from './lib.mjs';

const { ok, finish } = collector('temporal E2E');

await assertPreconditions([
	['audio fixture', `${BASE}/e2e/tone.wav`],
	['video fixture', `${BASE}/e2e/clip.mp4`],
]);
seed();

const browser = await launchBrowser();
const page = await browser.newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));

const count = async () => {
	const t = await page
		.locator('[title="Annotation count"]')
		.first()
		.textContent()
		.catch(() => null);
	return t ? parseInt(t.trim(), 10) : NaN;
};
async function countAbove(before, timeoutMs = 5000) {
	const t0 = Date.now();
	for (;;) {
		const c = await count();
		if (Number.isFinite(c) && c > before) return c;
		if (Date.now() - t0 > timeoutMs) return c;
		await page.waitForTimeout(150);
	}
}
/** The demo unit's annotation rows, fetched node-side (Arrow IPC → objects). */
async function fetchRows() {
	const res = await fetch(`${API}/api/annotations/${KEY}`);
	const table = tableFromIPC(new Uint8Array(await res.arrayBuffer()));
	return table.toArray().map((r) => ({ ...r }));
}

async function suite() {
	// ════ AUDIO — waveform · drag-create · region-resize · save · review times ════
	await page.goto(`${BASE}/annotator?keys=${KEY}&kind=audio&media=/e2e/tone.wav`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(1000);
	const status = await page
		.locator('[data-testid="annotate-status"]')
		.textContent()
		.catch(() => '');
	ok('audio unit resolves (kind=audio)', /annotate · audio/.test(status ?? ''), status ?? '');

	// wavesurfer decoded + mounted (its waveform div appears; the hint text = edit mode)
	const hint = await page.getByText('Drag on the waveform to add a segment').count();
	ok('waveform mounts in edit mode', hint > 0);
	const wave = await page
		.locator('div:has(> div > wave), div.bg-card:has(canvas)')
		.last()
		.boundingBox()
		.catch(() => null);
	const waveBox =
		wave ??
		(await page
			.locator('canvas')
			.last()
			.boundingBox()
			.catch(() => null));
	ok('waveform canvas rendered', !!waveBox && waveBox.width > 100);
	if (!waveBox) throw new Error('waveform never rendered');

	// drag-create a segment across the middle of the waveform
	const before = await count();
	const y = waveBox.y + waveBox.height / 2;
	await page.mouse.move(waveBox.x + waveBox.width * 0.2, y);
	await page.mouse.down();
	await page.mouse.move(waveBox.x + waveBox.width * 0.5, y, { steps: 8 });
	await page.mouse.up();
	const afterCreate = await countAbove(before);
	ok('drag on waveform creates a segment', afterCreate > before, `${before} → ${afterCreate}`);

	// the review list shows the segment's time range (m:ss–m:ss)
	const range = await page
		.locator('[data-testid="annotation-sidebar"]')
		.getByText(/\d:\d\d–\d:\d\d/)
		.count();
	ok('segment time range shows in the review list', range > 0);

	// resize: drag the region's right edge further right (wavesurfer region handle)
	const region = page.locator('[part*="region"]').first();
	const regionBox = await region.boundingBox().catch(() => null);
	ok('wavesurfer region rendered', !!regionBox);
	if (regionBox) {
		await page.mouse.move(regionBox.x + regionBox.width - 2, regionBox.y + regionBox.height / 2);
		await page.mouse.down();
		await page.mouse.move(
			Math.min(regionBox.x + regionBox.width + waveBox.width * 0.25, waveBox.x + waveBox.width - 4),
			regionBox.y + regionBox.height / 2,
			{ steps: 8 },
		);
		await page.mouse.up();
		await page.waitForTimeout(400);
	}

	// save → verify the temporal row round-tripped to Lance with a real time range
	const saved = page
		.waitForResponse(
			(r) => r.url().includes('/api/annotations/') && r.request().method() === 'POST',
			{ timeout: 10000 },
		)
		.then((r) => r.ok())
		.catch(() => false);
	await page.keyboard.press('Control+s');
	ok('audio save POSTs', await saved);
	await page.waitForTimeout(700);
	const rows = await fetchRows();
	const seg = rows.find((r) => r.shape_type === 'segment');
	ok(
		'segment row persisted with t_start<t_end',
		!!seg && seg.t_end > seg.t_start,
		seg ? `t=[${seg.t_start.toFixed(2)}, ${seg.t_end.toFixed(2)}]` : '(no segment row)',
	);
	const resized = !!seg && seg.t_end - seg.t_start > 0.05;
	ok('segment times are real (resize round-trip)', resized);

	// reload → the segment survives + still listed
	await page.goto(`${BASE}/annotator?keys=${KEY}&kind=audio&media=/e2e/tone.wav`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(1200);
	const afterReload = await count();
	ok('audio segment persists across reload', afterReload >= afterCreate, `count ${afterReload}`);

	// ════ VIDEO — frame under overlay · scrub · draw-on-frame pinned at playhead ════
	seed(); // fresh table for the video leg
	await page.goto(`${BASE}/annotator?keys=${KEY}&kind=video&media=/e2e/clip.mp4`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(1500);
	const vstatus = await page
		.locator('[data-testid="annotate-status"]')
		.textContent()
		.catch(() => '');
	ok('video unit resolves (kind=video)', /annotate · video/.test(vstatus ?? ''), vstatus ?? '');

	const canvas = await page
		.locator('canvas')
		.first()
		.boundingBox()
		.catch(() => null);
	ok('video frame canvas mounted', !!canvas && canvas.width > 100);
	if (!canvas) throw new Error('video canvas never mounted');

	// scrub to t≈2s through the real <video> element (fires seeked → frame snapshot)
	await page.evaluate(() => {
		const v = document.querySelector('video');
		v.currentTime = 2.0;
	});
	await page.waitForTimeout(900);
	const t = await page.evaluate(() => document.querySelector('video')?.currentTime ?? -1);
	ok('scrub seeks the video (t≈2s)', Math.abs(t - 2.0) < 0.25, `currentTime=${t.toFixed(2)}`);

	// draw a rect on the paused frame → pinned at the playhead
	const vbefore = await count();
	await page.mouse.move(canvas.x + canvas.width / 2, canvas.y + canvas.height / 2);
	await page.keyboard.press('3'); // rect
	await page.waitForTimeout(200);
	const x1 = canvas.x + canvas.width * 0.3,
		y1 = canvas.y + canvas.height * 0.3;
	await page.mouse.move(x1, y1);
	await page.mouse.down();
	await page.mouse.move(x1 + 80, y1 + 60, { steps: 8 });
	await page.mouse.up();
	const vafter = await countAbove(vbefore);
	ok('draw rect on the paused frame', vafter > vbefore, `${vbefore} → ${vafter}`);

	const vsaved = page
		.waitForResponse(
			(r) => r.url().includes('/api/annotations/') && r.request().method() === 'POST',
			{ timeout: 10000 },
		)
		.then((r) => r.ok())
		.catch(() => false);
	await page.keyboard.press('Control+s');
	ok('video save POSTs', await vsaved);
	await page.waitForTimeout(700);
	const vrows = await fetchRows();
	const pinned = vrows.find(
		(r) => r.shape_type === 'rectangle' && r.t_start > 1.5 && r.t_start < 2.5,
	);
	ok(
		'frame shape pinned at the playhead (t_start≈2s)',
		!!pinned,
		pinned ? `t_start=${pinned.t_start.toFixed(2)}` : '(no pinned rect)',
	);

	// reload → persists
	await page.goto(`${BASE}/annotator?keys=${KEY}&kind=video&media=/e2e/clip.mp4`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(1500);
	ok('video shape persists across reload', (await count()) >= vafter, `count ${await count()}`);

	ok('no page-level JS errors', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
}

let crashed = null;
try {
	await suite();
} catch (e) {
	crashed = e;
	console.error('SUITE CRASH:', e?.message ?? e);
} finally {
	await browser.close().catch(() => {});
	try {
		seed();
	} catch {
		console.error('WARNING: post-run re-seed failed — run `make seed-annotations` manually');
	}
	finish(crashed);
}
