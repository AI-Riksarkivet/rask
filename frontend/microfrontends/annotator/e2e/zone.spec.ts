import { test, expect, type Route } from '@playwright/test';

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

// --------------------------------------------------------------------------------------------------
// AI assist — the model's shapes ride the review path (the coverage the live drive proved manually)
// --------------------------------------------------------------------------------------------------

test('a real-runner Detect drops a prediction the reviewer can Accept — never an auto-annotation', async ({
	page,
}) => {
	// A runner IS deployed (LIFO beats the beforeEach mock)…
	await page.route('**/annotator/api/config', (route) =>
		json(route, { assistRunner: true, jobsRunner: false }),
	);
	// The unit LOADS (an empty Arrow table + version): assist requires a loaded unit — a
	// prediction that could never be saved would be a lie, so the guard is the app's, not ours.
	const EMPTY_ARROW = Buffer.from(
		'/////5ADAAAQAAAAAAAKABAADgAHAAgACgAAAAAAAAEQAAAAAAAEAAgACAAAAAQACAAAAAQAAAARAAAANAMAAOwCAAC8AgAAkAIAAGQCAAA0AgAABAIAAKgBAAB4AQAASAEAABwBAADwAAAAxAAAAJgAAABsAAAAOAAAAAQAAAAg/f//EAAAABwAAAAAAAADGAAAAAsAAAB1bmNlcnRhaW50eQAAAAAAVv3//wAAAgBQ/f//EAAAABwAAAAAAAADGAAAAAoAAABjb25maWRlbmNlAAAAAAAAhv3//wAAAgCA/f//EAAAABgAAAAAAAAFFAAAAAUAAABncm91cAAAAAAAAAB0/f//qP3//xAAAAAYAAAAAAAABRQAAAAGAAAAc291cmNlAAAAAAAAnP3//9D9//8QAAAAGAAAAAAAAAUUAAAABgAAAHN0YXR1cwAAAAAAAMT9///4/f//EAAAABgAAAAAAAAFFAAAAAQAAAB0ZXh0AAAAAAAAAADs/f//IP7//xAAAAAYAAAAAAAABRQAAAAFAAAAbGFiZWwAAAAAAAAAFP7//0j+//8QAAAAGAAAAAAAAAMUAAAABQAAAHRfZW5kAAAAAAAAAHr+//8AAAIAdP7//xAAAAAYAAAAAAAAAxQAAAAHAAAAdF9zdGFydAAAAAAApv7//wAAAgCg/v//EAAAABgAAAAAAAAMRAAAAAcAAABwb2x5Z29uAAEAAAAEAAAAyP7//xAAAAAYAAAAAAAAAxQAAAAEAAAAaXRlbQAAAAAAAAAA+v7//wAAAgDE/v//+P7//xAAAAAYAAAAAAAAAxQAAAAGAAAAaGVpZ2h0AAAAAAAAKv///wAAAgAk////EAAAABgAAAAAAAADFAAAAAUAAAB3aWR0aAAAAAAAAABW////AAACAFD///8QAAAAFAAAAAAAAAMQAAAAAQAAAHkAAAAAAAAAfv///wAAAgB4////EAAAABQAAAAAAAADEAAAAAEAAAB4AAAAAAAAAKb///8AAAIAoP///xAAAAAcAAAAAAAABRgAAAAKAAAAc2hhcGVfdHlwZQAAAAAAAJj////M////EAAAABgAAAAAAAADHAAAAAYAAABudW1iZXIAAAAAAAAAAAYACAAGAAYAAAAAAAIAEAAUAAQAAAAPABAAAAAIABAAAAAQAAAAFAAAAAAAAAUUAAAAAgAAAGlkAAAAAAAABAAEAAQAAAD/////AAAAAA==',
		'base64',
	);
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() !== 'GET') return json(route, { detail: 'unstubbed write' }, 404);
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: EMPTY_ARROW,
		});
	});
	// …and the model answers one detected box, exactly the runner contract's shape.
	const assistCalls: unknown[] = [];
	await page.route('**/annotator/api/assist/**', (route) => {
		assistCalls.push(route.request().postDataJSON());
		return json(route, {
			shapes: [
				{
					shape_type: 'rectangle',
					x: 60,
					y: 90,
					width: 780,
					height: 1020,
					label: 'text',
					confidence: 0.75,
				},
			],
			source: 'model:grounding-dino',
		});
	});

	await page.goto(`/annotator/?dataset=demo&keys=${encodeURIComponent(KEY)}`);
	await expect(page.getByTestId('assist-mock-chip')).not.toBeVisible();

	await page.getByPlaceholder(/AI detect/).fill('text');
	await page.getByRole('button', { name: 'Run', exact: true }).click();

	// The POST carried the producer + the prompt — the contract, not a lookalike.
	await expect.poll(() => assistCalls.length, { timeout: 10_000 }).toBeGreaterThan(0);
	expect(assistCalls[0]).toMatchObject({ producer: 'grounding-dino', prompt: 'text' });

	// The shape arrives as a PREDICTION status chip — model output is never auto-accepted.
	await expect(page.getByText(/prediction\s*1/)).toBeVisible();
	const row = page.getByTestId('annotation-list').getByRole('button').last();
	await expect(row).toBeVisible();

	// The reviewer selects it and accepts; the pending count drains.
	await row.click();
	await page.getByRole('button', { name: /Accept/ }).click();
	await expect(page.getByText(/accepted\s*1/)).toBeVisible();
	await expect(page.getByText(/prediction\s*1/)).not.toBeVisible();
});
