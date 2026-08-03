import { test, expect, type Page, type Route } from '@playwright/test';
import { MOCK_ANNOTATOR } from '../ports';

// The two AI-assist behaviours that only mean anything when a model runner IS deployed. Runner
// presence is this app server's own env (`MEDIA_ASSIST_URL` → the `zoneConfig` remote query), not a
// fetch a browser can restub — so these run against the config's SECOND app server, which is started
// with it set. On the default server both tests would pass vacuously: the chip is up anyway.
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

test('assist chip stays up (fail-honest) when the config read fails', async ({ page }) => {
	// A runner IS deployed here, so a WORKING config hides the chip — which is what makes this test
	// meaningful: break the config read and the warning must survive, because mock is the stack's
	// default state and passing mock shapes off as model output is the failure this guards.
	await page.route('**/_app/remote/**', (route) => json(route, { detail: 'boom' }, 500));
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('assist-mock-chip')).toBeVisible();
});

test('a real-runner Detect drops a prediction the reviewer can Accept — never an auto-annotation', async ({
	page,
}) => {
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
