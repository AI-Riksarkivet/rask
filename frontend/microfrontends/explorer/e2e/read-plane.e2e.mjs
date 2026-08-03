/**
 * Read-plane E2E — the front page (WebGPU adapter, SavedViews save/apply) and the
 * annotator's provenance surfaces (compare-versions History panel fetching /versions).
 * Graduated from the session scratchpad smoke into the committed suite. Same
 * preconditions as the annotator suite (see lib.mjs / TESTING.md).
 */
// NOTE: `waitUntil: 'networkidle'` is deliberately absent from this file. Every zone's shell now holds a
// LIVE `query.live` stream open for the notification bell, so these apps have no idle network by design —
// the wait would sit until its timeout on every navigation. Each drive waits on the ELEMENT it is about
// to act on instead, which is the condition it actually needs.
import { BASE, KEY, assertPreconditions, collector, launchBrowser, seed } from './lib.mjs';

const { ok, finish } = collector('read-plane E2E');

await assertPreconditions();
seed();

const browser = await launchBrowser();
const page = await browser.newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));

async function suite() {
	// ── front page: WebGPU + SavedViews ──
	await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
	// Adapter acquisition can transiently fail right after another browser instance
	// released the GPU (Vulkan device contention) — retry on a FRESH page before
	// declaring WebGPU broken. This is the suite's only explicit WebGPU assertion
	// (Pixi would silently fall back), so it must be robust, not deleted.
	let gpu = 'unattempted';
	for (let attempt = 0; attempt < 3 && gpu !== 'adapter-ok'; attempt++) {
		if (attempt > 0) await page.waitForTimeout(1500);
		const probe = attempt === 0 ? page : await browser.newPage();
		if (probe !== page)
			await probe.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
		gpu = await probe.evaluate(async () => {
			if (!navigator.gpu) return 'no-navigator.gpu';
			try {
				const a = await navigator.gpu.requestAdapter();
				return a ? 'adapter-ok' : 'no-adapter';
			} catch (e) {
				return 'err:' + e.message;
			}
		});
		if (probe !== page) await probe.close();
	}
	ok('WebGPU adapter available', gpu === 'adapter-ok', gpu);
	await page.waitForTimeout(1200);

	const viewsBtn = page.locator('button:has-text("Views")').first();
	ok('SavedViews control renders', (await viewsBtn.count()) > 0);
	// Type a query FIRST so the saved view carries state the bar must resync to.
	const searchInput = page.locator('input[type="search"]').first();
	await searchInput.fill('e2e-resync');
	await page.keyboard.press('Enter');
	await page.waitForTimeout(600);
	await viewsBtn.click();
	await page.waitForTimeout(300);
	const saveInput = page.locator('input[placeholder*="Save current view"]');
	ok('SavedViews dropdown opens', (await saveInput.count()) > 0);

	await saveInput.fill('e2e-view');
	await page.keyboard.press('Enter');
	await page.waitForTimeout(300);
	const savedRow = page.locator('li', { hasText: 'e2e-view' }).first();
	ok('view saves + lists', (await savedRow.count()) > 0);
	// SearchBar resync: dirty the input WITHOUT submitting, apply the view — the
	// bar must adopt the applied spec (no force-remount hack anymore).
	await searchInput.fill('dirty-unsubmitted');
	// Popover state after the fill is toggle-order-dependent — ensure it's open.
	if (!(await saveInput.isVisible().catch(() => false))) {
		await page.locator('button:has-text("Views")').first().click();
		await page.waitForTimeout(300);
	}
	await savedRow.locator('button').first().click(); // apply → runSearch
	await page.waitForTimeout(600);
	ok('SearchBar resyncs on view apply', (await searchInput.inputValue()) === 'e2e-resync');
	// delete it again (leave localStorage clean)
	await page.locator('button:has-text("Views")').first().click();
	await page.waitForTimeout(200);
	const delBtn = page.locator('li', { hasText: 'e2e-view' }).locator('button[title="Delete view"]');
	if (await delBtn.count()) await delBtn.first().click();
	ok('view deletes', (await page.locator('li', { hasText: 'e2e-view' }).count()) === 0);

	// ── annotator provenance: the History panel fetches /versions + lists rows ──
	await page.goto(`${BASE}/annotator?keys=${KEY}`, {
		waitUntil: 'domcontentloaded',
		timeout: 60000,
	});
	await page.waitForTimeout(2000);
	const history = page.locator('[data-testid="version-history"]');
	ok('History panel present', (await history.count()) > 0);
	const versionsFetched = page
		.waitForResponse((r) => r.url().includes('/versions'), { timeout: 10000 })
		.then(() => true)
		.catch(() => false);
	await history.locator('button').first().click();
	ok('History expand fetches /versions', await versionsFetched);
	await page.waitForTimeout(500);
	const vrows = await history.locator('button', { hasText: /^v\d+/ }).count();
	ok('History lists version rows', vrows > 0, `${vrows} rows`);

	// ── workflow un-tag sync: inline tag → Save (adds) → un-tag → Save (removes) ──
	await page.goto(`${BASE}/workflow`, { waitUntil: 'domcontentloaded', timeout: 60000 });
	// Detach the FTS node from the vector chain (scene needs the embedding model
	// server, not part of this suite's preconditions) — FTS+backend is deterministic.
	for (let i = 0; i < 3 && (await page.locator('[data-id="e-scene-said"]').count()) > 0; i++) {
		await page.locator('[data-id="e-scene-said"] .svelte-flow__edge-path').click({ force: true });
		await page.keyboard.press('Delete');
		await page.waitForTimeout(300);
	}
	ok('scene edge detached', (await page.locator('[data-id="e-scene-said"]').count()) === 0);
	await page.locator('button:has-text("Run")').first().click();
	const addTagBtn = page.getByRole('button', { name: 'tag', exact: true }).first();
	await addTagBtn.waitFor({ state: 'visible', timeout: 30000 });
	await addTagBtn.click();
	await page.locator('input[placeholder="tag…"]').fill('e2e-untag');
	await page.keyboard.press('Enter');

	// Wait on the RESPONSE (the Lance commit is done before it returns) — no fixed
	// sleeps between save and the version read, and the ok() status is asserted too.
	const savePost = async () => {
		const captured = page.waitForResponse(
			(r) => r.url().includes('/api/annotations/tags') && r.request().method() === 'POST',
			{ timeout: 15000 },
		);
		await page.locator('button:has-text("as annotations")').first().click();
		const resp = await captured;
		if (!resp.ok()) throw new Error(`tags save HTTP ${resp.status()}`);
		return JSON.parse(resp.request().postData() ?? '{}');
	};
	const addBody = await savePost();
	ok(
		'tag save sends adds',
		addBody.adds?.some((a) => a.labels.includes('e2e-untag')),
	);
	const unit = addBody.adds?.find((a) => a.labels.includes('e2e-untag'));
	if (!unit) throw new Error('no tagged unit in the adds payload — cannot verify server side');
	const vUrl = `${BASE}/api/annotations/${unit.doc_id}/${unit.keys.join('/')}/versions`;
	// /versions returns newest-first — [0] is the latest version's per-unit row count.
	const countAt = async () => (await (await fetch(vUrl)).json())[0].count;
	const afterAdd = await countAt();

	await page.locator('button[aria-label="Remove tag e2e-untag"]').first().click();
	const removeBody = await savePost();
	ok(
		'un-tag save sends removes leg',
		removeBody.removes?.some((r) => r.labels.includes('e2e-untag')),
	);
	const afterRemove = await countAt();
	ok('tag row removed server-side', afterRemove === afterAdd - 1, `${afterAdd}→${afterRemove}`);

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
		console.error('WARNING: post-run re-seed failed');
	}
	finish(crashed);
}
