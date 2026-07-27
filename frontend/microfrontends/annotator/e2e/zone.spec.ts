import { test, expect, type Route } from '@playwright/test';

// Hermetic coverage for the zone contract: the app is server-aware (hooks + BFF routes
// answer under /annotator; only the Pixi canvas page itself opts out of SSR per-page),
// the client fetches the media plane through THIS zone's base-prefixed BFF routes
// (/annotator/api/*), and the landing is the DATA-SELECTION view (datasets → documents
// → chunks → canvas) with `?keys=` deep links opening the canvas directly.

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
	await page.route('**/annotator/api/config', (route) =>
		json(route, { assistRunner: false, jobsRunner: false }),
	);
	await page.route('**/annotator/api/datasets', (route) => json(route, DATASETS));
	await page.route('**/annotator/api/health', (route) => json(route, HEALTH));
	await page.route('**/annotator/api/datasets/demo/descriptor', (route) => json(route, DESCRIPTOR));
	await page.route('**/annotator/api/documents*', (route) => json(route, DOCUMENTS));
	await page.route(`**/annotator/api/doc-transcript/${DOC}*`, (route) => json(route, TRANSCRIPT));
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

test('landing = data selection: dataset → document → chunk → the annotate canvas', async ({
	page,
}) => {
	await page.goto('/annotator/');
	// No hardcoded demo unit: the selection view renders (single dataset auto-picked).
	await expect(page.getByTestId('data-selection')).toBeVisible();
	await expect(page.getByTestId('dataset-id')).toHaveText('demo');
	// Documents grid (thumbnails via the zone BFF) → drill into the chunk picker.
	await page.getByTestId('doc-tile').click();
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
	// The rail's back button returns to the selection view.
	await page.getByTestId('exit-annotate').click();
	await expect(page.getByTestId('data-selection')).toBeVisible();
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

test('AI assist is labeled mocked while no model runner is deployed', async ({ page }) => {
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('assist-mock-chip')).toBeVisible();
	await expect(page.getByTestId('assist-mock-chip')).toContainText('mocked — needs runner');
});

test('assist chip stays up (fail-honest) when the config fetch fails', async ({ page }) => {
	// Mock is the stack's default state — an unreachable /api/config must NOT silently
	// drop the chip and pass mock shapes off as model output.
	await page.route('**/annotator/api/config', (route) => json(route, { detail: 'boom' }, 500));
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('assist-mock-chip')).toBeVisible();
});
