import { test, expect, type Route } from '@playwright/test';

import { MOCK_ANNOTATOR } from './ports';

// Hermetic coverage for the zone contract: the app is server-aware (hooks + BFF routes
// answer under /annotator; only the Pixi canvas page itself opts out of SSR per-page),
// the client fetches the media plane through THIS zone's base-prefixed BFF routes
// (/annotator/api/*). Since S9 the LANDING is the PROJECTS view (e2e/projects.spec.ts);
// the data-selection browser lives at /annotator/browse, and `?keys=` deep links still
// open the canvas directly.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// 1×1 transparent PNG for thumbnails + chunk frames.
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;

// A minimal-but-real corpus for the selection flow (viewer-BFF shapes).
const DATASETS = {
	datasets: [
		{
			id: 'demo',
			tables: { documents: { row_count: 1, version: 1 }, chunks: { row_count: 1, version: 1 } },
			capabilities: ['frames'],
		},
	],
};
const HEALTH = {
	db: { path: '/corpus/demo.lance', tables: ['documents', 'chunks'], chunks: 1, documents: 1 },
	embed: { ok: true, url: '', error: null },
	rerank: { ok: true, url: '', error: null },
};
const DESCRIPTOR = {
	id: 'demo',
	tables: {
		documents: { name: 'documents', row_count: 1, version: 1, columns: [], indexes: [] },
		chunks: { name: 'chunks', row_count: 1, version: 1, columns: [], indexes: [] },
	},
	declared: {
		identity: { key_fields: ['doc_id', 'speech_id', 'chunk_id'], doc_key: 'doc_id' },
		document: { table: 'documents', media_blob: 'media', thumbnail: 'thumb' },
		time: { start: 't0', end: 't1' },
		display: { title: ['namn'], body: 'text' },
		search: { row_table: 'chunks' },
	},
};
const DOCUMENTS = {
	total: 1,
	page: 1,
	docs: [{ doc_id: DOC, namn: 'Demo document', duration: 12 }],
};
// Chunk rows carry only the NON-doc identity fields — the backend strips the doc key
// (it is the path parameter). The picker must stamp it back in (live-found bug).
const TRANSCRIPT = {
	doc_id: DOC,
	chunks: [{ speech_id: 0, chunk_id: 19, t0: 0, t1: 2.5, text: 'hello world' }],
};

let apiPaths: string[] = [];
let apiWrites: string[] = [];

test.beforeEach(async ({ page }) => {
	apiPaths = [];
	apiWrites = [];
	// Zone-scoped globs on purpose: a bare **/api/** also matches Vite /@fs module URLs
	// (…/packages/api/…) and would kill hydration. Registration order is LIFO — the
	// generic 404 first, the specific mocks after so they win.
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => {
		const req = route.request();
		const u = new URL(req.url());
		apiPaths.push(u.pathname + u.search); // query included: the dataset selector must ride
		if (req.method() !== 'GET') apiWrites.push(u.pathname);
		return json(route, { detail: 'unstubbed' }, 404);
	});
	// No `/api/config` mock any more: the runner-presence signal is a remote query reading this
	// server's own env (the transport ruling, area 4), and THIS server is started with no runner — which
	// is exactly the honest-mock state the chip specs below assert. The real-runner path needs a
	// different server env, so it lives in e2e/runner/.
	await page.route('**/annotator/api/datasets', (route) => json(route, DATASETS));
	await page.route('**/annotator/api/health', (route) => json(route, HEALTH));
	await page.route('**/annotator/api/datasets/demo/descriptor', (route) => json(route, DESCRIPTOR));
	await page.route('**/annotator/api/documents*', (route) => json(route, DOCUMENTS));
	await page.route(`**/annotator/api/doc-chunks/${DOC}*`, (route) => json(route, TRANSCRIPT));
	await page.route('**/annotator/api/thumbnail/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	await page.route('**/annotator/api/chunk-frame/**', (route) => {
		const u = new URL(route.request().url());
		apiPaths.push(u.pathname + u.search);
		return route.fulfill({ status: 200, contentType: 'image/png', body: PNG });
	});
	// The annotations GET 404s (the generic route): the viewer's documented failure
	// surface — the shell boots and the status chip reports the failure honestly.
});

test('the server answers under /annotator (hooks live; canvas page opts out per-page)', async ({
	page,
}) => {
	const res = await page.request.get('/annotator/');
	expect(res.status()).toBe(200);
	const html = await res.text();
	// The page itself is a per-page ssr=false island, but it is SERVED by the SvelteKit
	// server under the zone base — the kit-injected config must carry the based path
	// (dev serves modules at /annotator/@fs, the build at /annotator/_app).
	expect(html).toContain('base: "/annotator"');
	// Every app.html placeholder must have been substituted. SvelteKit string-replaces these
	// tokens ANYWHERE in the file, comments included — writing one inside a comment silently
	// consumes the real head (stylesheet link and all) and leaves the live placeholder to
	// render as visible text. Caught that way once; this keeps it caught.
	expect(html).not.toContain('%sveltekit.');
});

test('the zone follows the ESTATE theme, not a zone-private one', async ({ page }) => {
	// The regression this guards: the annotator used to pin `class="dark"` on <html> and read
	// its own `lance-media-theme` key, so it stayed dark while the rest of the estate rendered
	// light and the navbar's theme toggle did nothing here. It now reads the SAME origin-wide
	// `mode-watcher-mode` key every other zone reads — set anywhere, honoured here, before
	// first paint (this zone inlines the boot script; its canvas route is ssr=false).
	await page.addInitScript(() => localStorage.setItem('mode-watcher-mode', 'light'));
	await page.goto('/annotator/');
	await expect(page.locator('html')).not.toHaveClass(/dark/);

	await page.addInitScript(() => localStorage.setItem('mode-watcher-mode', 'dark'));
	await page.goto('/annotator/');
	await expect(page.locator('html')).toHaveClass(/dark/);
});

test('S9: the landing is PROJECTS; /browse hosts the data selection → canvas flow', async ({
	page,
}) => {
	// The landing is the projects view now (its own flows live in projects.spec.ts — here it
	// only has to BE the landing, in any load state).
	await page.goto('/annotator/');
	await expect(page.getByRole('heading', { name: 'Labeling tasks' })).toBeVisible();
	await expect(page.getByTestId('data-selection')).not.toBeVisible();

	// The selection flow moved to /browse, intact: dataset → document → chunk → canvas.
	await page.goto('/annotator/browse');
	await expect(page.getByTestId('data-selection')).toBeVisible();
	await expect(page.getByTestId('dataset-id')).toHaveText('demo');
	// `doc-row`, not `doc-tile`: browse opens as a TABLE now (#70) with the gallery on a toggle,
	// because a bulk-labeling surface is filtered and sorted to a set before anything is picked —
	// a gallery answers "what does this look like" and the job here asks "which of these match".
	// The tile still exists and is exercised below via the gallery toggle.
	await page.getByTestId('doc-row').first().click();
	await expect(page.getByTestId('chunk-picker')).toBeVisible();
	// Open one chunk in the canvas: the URL carries the ?keys= deep link and the shell
	// boots, loading the unit through the zone-based BFF paths.
	await page.getByTestId('chunk-row').click();
	await expect(page).toHaveURL(new RegExp(`keys=${encodeURIComponent(KEY)}`));
	// The DEFAULT dataset's deep link stays byte-identical — no ?dataset= param.
	expect(page.url()).not.toContain('dataset=');
	await expect(page.getByTitle('Redo (Ctrl+Shift+Z)')).toBeVisible();
	await expect.poll(() => apiPaths).toContain(`/annotator/api/annotations/${KEY}`);
	expect(apiWrites).toHaveLength(0);
	// Exiting the canvas lands on the zone's landing — the projects view.
	await page.getByTestId('exit-annotate').click();
	await expect(page.getByRole('heading', { name: 'Labeling tasks' })).toBeVisible();
});

test('?keys= deep link opens the canvas directly (the read-plane bridge)', async ({ page }) => {
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTitle('Redo (Ctrl+Shift+Z)')).toBeVisible();
	await expect(page.getByTestId('data-selection')).not.toBeVisible();
	await expect.poll(() => apiPaths).toContain(`/annotator/api/annotations/${KEY}`);
	expect(apiWrites).toHaveLength(0);
});

test('?dataset= deep link targets the picked dataset (frame + annotations carry it)', async ({
	page,
}) => {
	// A NON-default dataset picked in the selection view rides the deep link; the canvas
	// must fetch frame AND annotations (the save/versions endpoint) with ?dataset= — not
	// silently the default dataset's.
	await page.goto(`/annotator/?dataset=other&keys=${KEY}`);
	await expect(page.getByTitle('Redo (Ctrl+Shift+Z)')).toBeVisible();
	await expect.poll(() => apiPaths).toContain(`/annotator/api/annotations/${KEY}?dataset=other`);
	await expect.poll(() => apiPaths).toContain(`/annotator/api/chunk-frame/${KEY}?dataset=other`);
	expect(apiWrites).toHaveLength(0);
});

test('unreachable annotations surface on the status chip (no silent loading hang)', async ({
	page,
}) => {
	await page.goto(`/annotator/?keys=${KEY}`);
	// The stubbed annotations GET 404s — the chip must say so instead of hanging.
	await expect(page.getByTestId('annotate-status')).toContainText('load failed');
});

test('AI assist is NOT offered on a canvas that cannot accept shapes', async ({ page }) => {
	// This spec stubs the annotations GET to 404 for every test (see the status-chip test above), so
	// the canvas here is always read-only. Assist WRITES predicted shapes, so offering it on a
	// surface that would discard them is the same defect #72 fixed for the drawing tools — the bar is
	// gated on `controller.canDraw`, and the honesty chip lives inside it.
	//
	// This assertion replaced "AI assist is labeled mocked while no model runner is deployed", which
	// asserted that chip VISIBLE on this very page. That test could only ever have passed before the
	// gate existed; after it, its premise (a read-only canvas showing an assist affordance) is the
	// thing we now deliberately prevent. The mock-chip assertion belongs where the canvas is
	// writable — the runner-backed spec this file's own footer points at — and is tracked in #85.
	await page.goto(`/annotator/?keys=${KEY}`);

	// The canvas really did mount and really is read-only — otherwise "absent" proves nothing.
	await expect(page.getByTitle('Redo (Ctrl+Shift+Z)')).toBeVisible();
	await expect(page.getByTestId('annotate-status')).toContainText('load failed');

	await expect(page.getByTestId('assist-mock-chip')).toHaveCount(0);
	await expect(page.getByTestId('ai-assist')).toHaveCount(0);
});

// The FAIL-HONEST chip test and the real-runner Detect flow both need a server where a runner IS
// deployed (`MEDIA_ASSIST_URL` set) — presence is server env now, not a fetch the browser can restub.
// They live in e2e/runner/assist.spec.ts, driven against this config's second app server.

test('propagation exposes BOTH knobs and says what the cutoff EXCLUDED', async ({ page }) => {
	// #87 / `open_browse.md` §5. Propagating a label to neighbours is the highest-leverage action on
	// this surface and the easiest way to mislabel a corpus at scale: `n` alone says "give me forty"
	// whether or not forty are actually alike, so the fortieth gets the label because it was RETURNED,
	// not because it resembled anything. Both knobs must be VISIBLE and adjustable, and the cutoff has
	// to state what it removed — a threshold nobody can see is the whole failure mode.
	//
	// `findSimilar` is a REMOTE function: it runs on the zone SERVER, so `page.route` cannot intercept
	// it. It is seeded on the mock instead, which `SEARCH_API` now points at (playwright.config.ts).
	// Before that env existed this panel could only ever be driven into its error branch — which is
	// why the propagation controls had no browser coverage at all.
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /api/search/similar': [
					{ doc_id: DOC, speech_id: 0, chunk_id: 20, _distance: 0.1 },
					{ doc_id: DOC, speech_id: 0, chunk_id: 21, _distance: 0.2 },
					{ doc_id: DOC, speech_id: 0, chunk_id: 22, _distance: 0.9 },
				],
			},
		},
	});

	await page.goto('/annotator/browse');
	await page.getByTestId('doc-row').first().click();
	await expect(page.getByTestId('chunk-picker')).toBeVisible();
	await page.getByTestId('similar-open').first().click();

	// Both knobs RENDERED, not merely implemented.
	await expect(page.getByTestId('propagate-n')).toBeVisible();
	await expect(page.getByTestId('propagate-cutoff')).toBeVisible();

	// Wide open: everything returned is inside the cutoff, and the panel says so rather than leaving
	// the reader to infer it from a list length.
	await expect(page.getByTestId('propagate-summary')).toContainText('all 3 within the cutoff');

	// Tighten below the furthest neighbour. The summary must name the EXCLUDED one — "2 of 3" alone
	// would leave it invisible, which is exactly the silence this task exists to remove.
	await page.getByTestId('propagate-cutoff').fill('0.5');
	await expect(page.getByTestId('propagate-summary')).toContainText('1 beyond the cutoff');
	// …and it is genuinely gone from the LIST, not merely from the count.
	await expect(page.getByTestId('similar-list')).not.toContainText('0.900');
});
