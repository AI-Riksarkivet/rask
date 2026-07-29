/**
 * Annotator E2E regression suite — drives the REAL app in a real (headless, WebGPU)
 * Chromium: every drawing tool, the OpenCV tools, AI-assist (Detect + SAM), and
 * draw → save → persist. Failures here mean a user-visible feature broke.
 *
 * Preconditions (asserted at start):
 *   - the split services up (`make services-up`: viewer :8101 / search :8102 /
 *     annotator :8103) with MEDIA_ASSIST_URL UNSET — the AI-assist checks rely on the
 *     deterministic in-repo mock (services/annotator/api/v1/endpoints/assist.py); a live
 *     model endpoint would be nondeterministic.
 *   - the dev proxy at :5200 (`bun run dev` in frontend/ — all four zones)
 *   - a chromium with WebGPU: default = the ms-playwright cache; override with E2E_CHROME.
 *   - the demo unit (E2E_KEY, default fe00cd746463ad2c/0/19) present in the dataset.
 *
 * Run: `bun run test:e2e` (from apps/media). Re-seeds the demo annotations before + after,
 * so the suite is deterministic and leaves the demo clean.
 */
// NOTE: `waitUntil: 'networkidle'` is deliberately absent from this file. Every zone's shell now holds a
// LIVE `query.live` stream open for the notification bell, so these apps have no idle network by design —
// the wait would sit until its timeout on every navigation. Each drive waits on the ELEMENT it is about
// to act on instead, which is the condition it actually needs.
import { BASE, KEY, assertPreconditions, collector, launchBrowser, seed } from './lib.mjs';

const { ok, finish } = collector('annotator E2E');

await assertPreconditions();
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
/** Poll until the annotation count exceeds `before` (or time out) — replaces fixed
 *  settle sleeps so slow commits don't flake and fast ones don't waste time. */
async function countAbove(before, timeoutMs = 5000) {
	const t0 = Date.now();
	for (;;) {
		const c = await count();
		if (Number.isFinite(c) && c > before) return c;
		if (Date.now() - t0 > timeoutMs) return c;
		await page.waitForTimeout(150);
	}
}

// The suite body runs inside try/finally so ANY throw (a null canvas box, a locator
// timeout, a seed failure mid-run) still closes the browser, re-seeds the demo, and
// prints the summary with a non-zero exit — instead of crashing half-cleaned.
let box;
let cx;
let cy;
const pt = (fx, fy) => [box.x + box.width * fx, box.y + box.height * fy];
async function drag(ax, ay, bx, by, steps = 8) {
	await page.mouse.move(ax, ay);
	await page.mouse.down();
	await page.mouse.move((ax + bx) / 2, (ay + by) / 2, { steps: 3 });
	await page.mouse.move(bx, by, { steps });
	await page.mouse.up();
}
async function tool(key) {
	await page.mouse.move(cx, cy);
	await page.keyboard.press(key);
	await page.waitForTimeout(200);
}
/** Assert a gesture on tool `key` grows the annotation count by ≥1. */
async function draws(name, key, gesture) {
	await tool(key);
	const before = await count();
	await gesture();
	const after = await countAbove(before);
	ok(
		`tool '${name}' commits a shape`,
		Number.isFinite(after) && after > before,
		`${before} → ${after}`,
	);
}

async function suite() {
	await page.goto(`${BASE}/annotator?keys=${KEY}`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(2500);
	const status = await page
		.locator('[data-testid="annotate-status"]')
		.textContent()
		.catch(() => '');
	ok('annotator loads', /annotations from Lance/.test(status ?? ''), status ?? '');
	box = await page
		.locator('canvas')
		.first()
		.boundingBox()
		.catch(() => null);
	ok('canvas mounted', !!box && box.width > 100);
	if (!box) throw new Error('canvas never mounted — aborting the gesture checks');
	cx = box.x + box.width / 2;
	cy = box.y + box.height / 2;

	// ── simple drawing tools ──
	await draws('rect', '3', () => drag(...pt(0.3, 0.3), ...pt(0.42, 0.42)));
	await draws('point', '5', () => page.mouse.click(cx + 30, cy + 30));
	await draws('line', '6', () => drag(...pt(0.3, 0.6), ...pt(0.48, 0.63)));
	await draws('polygon', '4', async () => {
		const [px, py] = pt(0.55, 0.55);
		await page.mouse.click(px, py);
		await page.mouse.click(px + 40, py);
		await page.mouse.click(px + 40, py + 40);
		await page.keyboard.press('Enter');
	});
	// pencil: freehand drag commits a simplified polygon on release
	await draws('pencil', '8', async () => {
		const [px, py] = pt(0.6, 0.65);
		await page.mouse.move(px, py);
		await page.mouse.down();
		for (const [dx, dy] of [
			[30, 5],
			[55, 25],
			[40, 50],
			[10, 40],
		]) {
			await page.mouse.move(px + dx, py + dy, { steps: 4 });
		}
		await page.mouse.up();
	});
	// brush: strokes accumulate; Enter commits the mask
	await draws('brush', 'b', async () => {
		await drag(...pt(0.68, 0.3), ...pt(0.75, 0.38), 10);
		await page.keyboard.press('Enter');
	});

	// ── lasso is a SELECTION tool: loop around the canvas → annotations get selected ──
	await tool('7');
	{
		const [sx, sy] = pt(0.1, 0.1);
		const [ex, ey] = pt(0.9, 0.85);
		await page.mouse.move(sx, sy);
		await page.mouse.down();
		await page.mouse.move(ex, sy, { steps: 8 });
		await page.mouse.move(ex, ey, { steps: 8 });
		await page.mouse.move(sx, ey, { steps: 8 });
		await page.mouse.move(sx, sy, { steps: 8 });
		await page.mouse.up();
		await page.waitForTimeout(500);
		const detail = await page
			.locator('[data-testid="annotation-sidebar"]')
			.getByText('Back to list')
			.count();
		ok("tool 'lasso' selects enclosed annotations", detail > 0, 'detail pane opened');
		await page.keyboard.press('Escape'); // clear selection for the next tools
		await page.waitForTimeout(200);
	}

	// ── OpenCV magnetic tool: lazy 8MB wasm init, then corner-snap commits ──
	// (An IntelligentScissors edge-trace tool was removed 2026-07-21 — the opencv-js
	// binding pathfinds degenerately; see InteractionManager. Magnetic is the CV tool.)
	await tool('9'); // magnetic — arms lazy init (wasm + corner detection)
	const magReady = await page
		.waitForSelector('[data-testid="annotator-toolbar"] button[data-cvready="true"]', {
			timeout: 90000,
		})
		.then(() => true)
		.catch(() => false);
	ok('magnetic: OpenCV init completes', magReady);
	if (magReady) {
		// SNAP assertion: sweep the cursor across the (corner-rich) frame and require the
		// live snap indicator to engage — proving keypoints were detected AND the cursor
		// actually locked onto one, not just that a polygon committed.
		let snapped = false;
		outer: for (let fy = 0.2; fy <= 0.8; fy += 0.15) {
			for (let fx = 0.15; fx <= 0.85; fx += 0.1) {
				await page.mouse.move(...pt(fx, fy), { steps: 4 });
				const s = await page
					.locator('[data-testid="annotator-toolbar"] button[data-snapped="true"]')
					.count();
				if (s > 0) {
					snapped = true;
					break outer;
				}
			}
		}
		ok('magnetic: cursor SNAPS onto a detected corner', snapped);

		const before = await count();
		const [mx, my] = pt(0.35, 0.7);
		await page.mouse.click(mx, my);
		await page.mouse.click(mx + 50, my);
		await page.mouse.click(mx + 50, my + 40);
		await page.mouse.dblclick(mx + 50, my + 40);
		const after = await countAbove(before);
		ok(
			"tool 'magnetic' commits a corner-snapped polygon",
			Number.isFinite(after) && after > before,
			`${before} → ${after}`,
		);
	}

	// ── AI-assist: Detect (GroundingDINO text) + Segment (SAM box) ──
	const assistBar = page.locator('[data-testid="ai-assist"]');
	{
		const before = await count();
		await assistBar.getByText('Detect').click();
		await assistBar.locator('input').first().fill('text line');
		const called = page
			.waitForResponse((r) => r.url().includes('/api/assist/'), { timeout: 10000 })
			.then(() => true)
			.catch(() => false);
		await assistBar.getByText('Run').click();
		ok('AI-assist Detect calls /api/assist', await called);
		const after = await countAbove(before);
		ok('AI-assist Detect adds predictions', after > before, `${before} → ${after}`);
	}
	{
		await assistBar.getByText('Segment').click();
		await page.waitForTimeout(300);
		const before = await count();
		const called = page
			.waitForResponse((r) => r.url().includes('/api/assist/'), { timeout: 10000 })
			.then(() => true)
			.catch(() => false);
		await drag(...pt(0.15, 0.75), ...pt(0.28, 0.88), 6);
		ok('SAM Segment calls /api/assist', await called);
		const after = await countAbove(before);
		ok('SAM Segment adds a mask shape', after > before, `${before} → ${after}`);
		await assistBar.getByText('Detect').click(); // disarm SAM
	}

	// ── save → reload → everything persisted ──
	const beforeSave = await count();
	const saved = page
		.waitForResponse(
			(r) => r.url().includes('/api/annotations/') && r.request().method() === 'POST',
			{ timeout: 10000 },
		)
		.then((r) => r.ok())
		.catch(() => false);
	await page.keyboard.press('Control+s');
	ok('save POSTs the batch', await saved);
	await page.waitForTimeout(600);
	await page.goto(`${BASE}/annotator?keys=${KEY}`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(2500);
	const persisted = await count();
	ok(
		'all shapes persist across reload',
		Number.isFinite(persisted) && persisted === beforeSave,
		`${persisted} (expected ${beforeSave})`,
	);

	ok('no page-level JS errors', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
}

// ── run + ALWAYS clean up (browser, demo re-seed, summary + exit code) ──
let crashed = null;
try {
	await suite();
} catch (e) {
	crashed = e;
	console.error('SUITE CRASH:', e?.message ?? e);
} finally {
	await browser.close().catch(() => {});
	try {
		seed(); // leave the demo clean
	} catch {
		console.error('WARNING: post-run re-seed failed — run `make seed-annotations` manually');
	}
	finish(crashed);
}
