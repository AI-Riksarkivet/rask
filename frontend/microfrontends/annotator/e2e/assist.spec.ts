import { test, expect, type Page, type Route } from '@playwright/test';
import { MOCK_ANNOTATOR } from './ports';

// The AI-assist behaviours that only mean anything when a model runner IS deployed, plus the
// settings surface that says which runners exist at all.
//
// Runner presence used to be the APP SERVER's own env (`MEDIA_ASSIST_URL` → the `zoneConfig` remote
// query). No browser can restub server env, so exercising the deployed path meant a whole second dev
// server started with it set. It now comes from the annotator SERVICE — `GET /api/assist/producers`
// — which is why these run on the ordinary server and simply SEED the fact they need. That is also
// the honest topology: the service is what resolves and calls a backend, so it is what should be
// asked whether one is deployed.
//
// The assist call itself is a remote command since the transport ruling area 4, so it leaves from the
// zone SERVER and is seeded on / asserted through the mock annotator's ledger. The annotations plane
// beside it is still Arrow bytes on a `+server.ts` route, so it is still `page.route`-mocked — the
// two halves of the transport rule, visible in one spec.

type Body = Record<string, unknown>;

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const KEY = 'fe00cd746463ad2c/0/19';

/** An EMPTY annotations table with the annotator's schema — assist requires a LOADED unit (a
 *  prediction that could never be saved would be a lie, so the guard is the app's, not ours). */
const EMPTY_ARROW = Buffer.from(
	'/////5ADAAAQAAAAAAAKABAADgAHAAgACgAAAAAAAAEQAAAAAAAEAAgACAAAAAQACAAAAAQAAAARAAAANAMAAOwCAAC8AgAAkAIAAGQCAAA0AgAABAIAAKgBAAB4AQAASAEAABwBAADwAAAAxAAAAJgAAABsAAAAOAAAAAQAAAAg/f//EAAAABwAAAAAAAADGAAAAAsAAAB1bmNlcnRhaW50eQAAAAAAVv3//wAAAgBQ/f//EAAAABwAAAAAAAADGAAAAAoAAABjb25maWRlbmNlAAAAAAAAhv3//wAAAgCA/f//EAAAABgAAAAAAAAFFAAAAAUAAABncm91cAAAAAAAAAB0/f//qP3//xAAAAAYAAAAAAAABRQAAAAGAAAAc291cmNlAAAAAAAAnP3//9D9//8QAAAAGAAAAAAAAAUUAAAABgAAAHN0YXR1cwAAAAAAAMT9///4/f//EAAAABgAAAAAAAAFFAAAAAQAAAB0ZXh0AAAAAAAAAADs/f//IP7//xAAAAAYAAAAAAAABRQAAAAFAAAAbGFiZWwAAAAAAAAAFP7//0j+//8QAAAAGAAAAAAAAAMUAAAABQAAAHRfZW5kAAAAAAAAAHr+//8AAAIAdP7//xAAAAAYAAAAAAAAAxQAAAAHAAAAdF9zdGFydAAAAAAApv7//wAAAgCg/v//EAAAABgAAAAAAAAMRAAAAAcAAABwb2x5Z29uAAEAAAAEAAAAyP7//xAAAAAYAAAAAAAAAxQAAAAEAAAAaXRlbQAAAAAAAAAA+v7//wAAAgDE/v//+P7//xAAAAAYAAAAAAAAAxQAAAAGAAAAaGVpZ2h0AAAAAAAAKv///wAAAgAk////EAAAABgAAAAAAAADFAAAAAUAAAB3aWR0aAAAAAAAAABW////AAACAFD///8QAAAAFAAAAAAAAAMQAAAAAQAAAHkAAAAAAAAAfv///wAAAgB4////EAAAABQAAAAAAAADEAAAAAEAAAB4AAAAAAAAAKb///8AAAIAoP///xAAAAAcAAAAAAAABRgAAAAKAAAAc2hhcGVfdHlwZQAAAAAAAJj////M////EAAAABgAAAAAAAADHAAAAAYAAABudW1iZXIAAAAAAAAAAAYACAAGAAYAAAAAAAIAEAAUAAQAAAAPABAAAAAIABAAAAAQAAAAFAAAAAAAAAUUAAAAAgAAAGlkAAAAAAAABAAEAAQAAAD/////AAAAAA==',
	'base64',
);

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_ANNOTATOR}/__mock/calls`);
	return ((await res.json()) as { calls: Body[] }).calls;
};

const assistCalls = async (page: Page): Promise<Body[]> =>
	(await calls(page)).filter((c) => String(c.path).startsWith('/api/assist/'));

/** Seed the SERVICE's registry — what "a model runner is deployed" now means to this zone. */
const seedProducers = (page: Page, producers: unknown[], defaultConfigured = false) =>
	page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /api/assist/producers': {
					producers,
					default_configured: defaultConfigured,
					urls_redacted: true,
				},
			},
		},
	});

const LIVE_DINO = { name: 'grounding-dino', configured: true, returns: ['bbox'], compatible: null };

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	// Zone-scoped glob on purpose: a bare **/api/** also matches Vite /@fs module URLs and would kill
	// hydration. Registration order is LIFO — the generic 404 first, the specific mocks after.
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
			headers: { 'X-Annotations-Version': '1' },
			body: EMPTY_ARROW,
		});
	});
});

test('assist chip stays up (fail-honest) when the registry read fails', async ({ page }) => {
	// A runner IS deployed here, so a WORKING read hides the chip — which is what makes this test
	// meaningful: break the read and the warning must survive, because mock is the stack's default
	// state and passing mock shapes off as model output is the failure this guards.
	await seedProducers(page, [LIVE_DINO]);
	await page.route('**/_app/remote/**', (route) => json(route, { detail: 'boom' }, 500));
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('assist-mock-chip')).toBeVisible();
});

test('an UNREACHABLE registry keeps the chip up too — not just a broken transport', async ({
	page,
}) => {
	// Nothing seeded ⇒ the mock service 404s the registry. That is the ordinary state of a stack with
	// no runner, and it must read as "mocked", never as "we could not tell, so assume it is real".
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('assist-mock-chip')).toBeVisible();
});

test('a real-runner Detect drops a prediction the reviewer can Accept — never an auto-annotation', async ({
	page,
}) => {
	await seedProducers(page, [LIVE_DINO]);
	// The model answers one detected box, exactly the runner contract's shape. Seeded WITHOUT the
	// query string so the dataset selector rides the wire without pinning the key here.
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				[`POST /api/assist/${KEY}`]: {
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
				},
			},
		},
	});

	await page.goto(`/annotator/?dataset=demo&keys=${encodeURIComponent(KEY)}`);
	await expect(page.getByTestId('assist-mock-chip')).not.toBeVisible();

	// Every model tool lives behind the ONE rainbow entry point now.
	await page.getByTestId('ai-assist-open').click();
	await page.getByPlaceholder(/AI detect/).fill('text');
	await page.getByRole('button', { name: 'Run', exact: true }).click();

	// The POST carried the producer + the prompt — the contract, not a lookalike — and it reached the
	// unit's OWN path, dataset selector included.
	await expect.poll(() => assistCalls(page), { timeout: 10_000 }).not.toHaveLength(0);
	const [first] = await assistCalls(page);
	expect(first!.body).toMatchObject({ producer: 'grounding-dino', prompt: 'text' });
	expect(first!.path).toBe(`/api/assist/${KEY}?dataset=demo`);

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

test('the settings panel names every backend, what it returns, and which are mocked', async ({
	page,
}) => {
	// The owner's question — "why is there no settings menu for which endpoints or runner is
	// configured.. and what they return" — answered in the UI. Three rows covering the three states
	// that matter, because a panel that renders only the happy one teaches nothing:
	//  · a LIVE backend whose output is known,
	//  · a MOCKED one (no endpoint resolves), which must be labelled as such here too,
	//  · a live one nobody knows anything about — the registry is open by design, so this happens,
	//    and "returns unknown" is the only honest thing to print.
	await seedProducers(page, [
		LIVE_DINO,
		{ name: 'sam', configured: false, returns: ['polygon'], compatible: null },
		{ name: 'yolo-world', configured: true, returns: [], compatible: null },
	]);
	await page.goto(`/annotator/?keys=${KEY}`);

	await page.getByTestId('assist-registry-trigger').click();
	const panel = page.getByTestId('assist-registry');
	await expect(panel).toBeVisible();

	await expect(panel.getByText('grounding-dino')).toBeVisible();
	await expect(panel.getByText('returns bbox')).toBeVisible();
	await expect(panel.getByText('returns polygon')).toBeVisible();
	// The endpoint is never printed — the service redacts it, and the panel must not reconstruct one.
	await expect(panel).not.toContainText('http');
	// A registered producer nobody knows must not be presented as if its output were understood.
	await expect(panel.getByText('returns unknown')).toBeVisible();
	// Per-producer mock state, which the single coarse chip cannot express.
	await expect(panel.getByText('mock', { exact: true })).toHaveCount(1);
});

test('a producer whose output the TASK refuses is flagged before anyone runs it', async ({
	page,
}) => {
	// "schemas must align right.. from the ml backend" — the whole point. A segmenter offered inside
	// a bbox-only task is answerable from config alone; the alternative is running the model,
	// reviewing its output, and being refused at submit.
	await seedProducers(page, [
		{ ...LIVE_DINO, compatible: true },
		{ name: 'sam', configured: true, returns: ['polygon'], compatible: false },
	]);
	await page.goto(`/annotator/?keys=${KEY}`);

	await page.getByTestId('assist-registry-trigger').click();
	const panel = page.getByTestId('assist-registry');

	await expect(panel.getByText('off-contract')).toBeVisible();
	await expect(panel.getByText('fits task')).toBeVisible();
});

test('a registry the service refuses to answer is NAMED, not rendered as an empty list', async ({
	page,
}) => {
	// "Nothing is configured" and "we could not ask" are different facts. A panel that shows an empty
	// list for both would report a working, empty registry on a broken transport.
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: { 'GET /api/assist/producers': { status: 503, body: { detail: 'upstream down' } } },
		},
	});
	await page.goto(`/annotator/?keys=${KEY}`);

	await page.getByTestId('assist-registry-trigger').click();
	await expect(page.getByTestId('assist-registry-error')).toContainText('upstream down');
});

test('the panel names what will ANSWER — live/mock per producer, task fit, backend returns', async ({
	page,
}) => {
	// One live backend, one mocked, compatibility computed against the task server-side — the
	// panel must wear all three facts per row instead of hiding them in the settings popover.
	await seedProducers(page, [
		{
			name: 'grounding-dino',
			configured: true,
			returns: ['bbox'],
			compatible: true,
			inputs: ['image + instruction prompt'],
		},
		{ name: 'sam', configured: false, returns: ['polygon'], compatible: false },
	]);
	await page.goto(`/annotator/?keys=${KEY}`);
	await page.getByTestId('ai-assist-open').click();

	// The ACTIVE MODEL is a dropdown; its contract line states input → output, live/mock, fit.
	const contract = page.getByTestId('assist-contract');
	await expect(page.getByTestId('assist-model-select')).toContainText('sam (mocked)');
	// The backend's DECLARED inputs win over the panel's family knowledge.
	await expect(contract).toContainText('image + instruction prompt');
	await expect(contract).toContainText('returns: bbox');
	await expect(contract.locator('[data-live="true"]')).toHaveCount(1);
	await expect(contract).toContainText('fits task');
	await expect(page.getByTestId('assist-detect')).toBeVisible();

	// Switching the model switches the CONTRACT and the input area with it.
	await page.getByTestId('assist-model-select').selectOption('sam');
	await expect(contract).toContainText('a click or a box');
	await expect(contract).toContainText('returns: polygon');
	await expect(contract.locator('[data-live="false"]')).toHaveCount(1);
	await expect(contract).toContainText('off-contract');
	await expect(page.getByTestId('arm-segment')).toBeVisible();
});

test('point clicks ACCUMULATE into one refined prediction — the session loop, on the wire', async ({
	page,
}) => {
	// The SAM convention: each click adds a signed point, the producer re-runs over the FULL
	// set, and the new answer REPLACES the old — a session refines ONE object, so N clicks must
	// never leave N stacked ghost masks in the review queue.
	await seedProducers(page, [
		{ name: 'sam', configured: true, returns: ['polygon'], compatible: null },
	]);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				[`POST /api/assist/${KEY}`]: {
					shapes: [
						{
							shape_type: 'polygon',
							x: 100,
							y: 100,
							width: 120,
							height: 120,
							polygon: [160, 100, 220, 160, 160, 220, 100, 160],
							label: 'seal',
							confidence: 0.8,
							uncertainty: 0.2,
						},
					],
					source: 'model:sam-click',
				},
			},
		},
	});

	await page.goto(`/annotator/?dataset=demo&keys=${encodeURIComponent(KEY)}`);
	await page.getByTestId('ai-assist-open').click();
	await page.getByTestId('assist-model-select').selectOption('sam');
	await page.getByTestId('arm-segment').click();

	const canvas = page.locator('canvas').first();
	await expect(canvas).toBeVisible();

	// First click: a one-point session.
	await canvas.click({ position: { x: 200, y: 200 }, force: true });
	await expect.poll(() => assistCalls(page), { timeout: 10_000 }).toHaveLength(1);
	const [first] = await assistCalls(page);
	const firstPoints = (first!.body as { points: Body[] }).points;
	expect(firstPoints).toHaveLength(1);
	expect(firstPoints[0]).toMatchObject({ positive: true });
	await expect(page.getByTestId('assist-point-count')).toContainText('1 point');
	await expect(page.getByText(/prediction\s*1/)).toBeVisible();

	// Second click REFINES: the request carries BOTH points (the first one verbatim), and the
	// queue still holds exactly ONE prediction — replaced, not stacked.
	await canvas.click({ position: { x: 260, y: 240 }, force: true });
	await expect.poll(() => assistCalls(page), { timeout: 10_000 }).toHaveLength(2);
	const second = (await assistCalls(page))[1]!;
	const secondPoints = (second.body as { points: Body[] }).points;
	expect(secondPoints).toHaveLength(2);
	expect(secondPoints[0]).toEqual(firstPoints[0]);
	await expect(page.getByTestId('assist-point-count')).toContainText('2 points');
	await expect(page.getByText(/prediction\s*1/)).toBeVisible();
	await expect(page.getByText(/prediction\s*2/)).not.toBeVisible();

	// The ± toggle signs the NEXT click as background.
	await page.getByTestId('assist-sign-negative').click();
	await canvas.click({ position: { x: 320, y: 300 }, force: true });
	await expect.poll(() => assistCalls(page), { timeout: 10_000 }).toHaveLength(3);
	const third = (await assistCalls(page))[2]!;
	const thirdPoints = (third.body as { points: Body[] }).points;
	expect(thirdPoints).toHaveLength(3);
	expect(thirdPoints[2]).toMatchObject({ positive: false });

	// Done keeps the prediction and ends the session — the pill returns to its resting copy.
	await page.getByTestId('assist-session-done').click();
	await expect(page.getByTestId('assist-point-count')).not.toBeVisible();
	await expect(page.getByTestId('assist-armed')).toContainText('Click or drag');
	await expect(page.getByText(/prediction\s*1/)).toBeVisible();
});

test('region tools are ABSENT on a text unit, with the reason on screen', async ({ page }) => {
	// Region-driven producers need a canvas to draw the region ON. A text document has none —
	// the section says why instead of offering a tool that cannot work there. Detect stays:
	// prompt-driven producers are model-dependent, not modality-dependent.
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() !== 'GET') return json(route, { detail: 'no' }, 404);
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: EMPTY_ARROW,
		});
	});
	await page.goto(`/annotator/?keys=${KEY}&kind=text`);
	await page.getByTestId('ai-assist-open').click();

	// The default (prompt-driven) model works on any media; a REGION model states why it cannot.
	await expect(page.getByTestId('assist-detect')).toBeVisible();
	await page.getByTestId('assist-model-select').selectOption('sam');
	await expect(page.getByTestId('assist-region')).toHaveCount(0);
	await expect(page.getByTestId('assist-region-absent')).toContainText('text');
});
