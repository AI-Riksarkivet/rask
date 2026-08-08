import { test, expect, type Route } from '@playwright/test';
import { MOCK_ANNOTATOR } from './ports';

// The toolbar's NEW controls — the two engine seams that shipped settable and unreachable, plus the
// scores a prediction now carries onto the sidebar.
//
//  · Brush radius/output: `BrushTool` took `radius` and `output: 'mask' | 'polygon'` from day one;
//    the eraser toggle was the only exposed control, so every stroke painted at the default 20 px
//    and always committed a raster mask.
//  · Image adjustments: `ImagePlugin.setImageAdjustments` had NO caller. Display-only — a faded
//    volume needs a contrast lift to trace, and that lift must never ride into anyone's data.
//  · Scores: the service's assist answer carries confidence + uncertainty; the client used to DROP
//    them, which left the "predictions first, highest uncertainty first" queue unrankable.
//
// Same hermetic split as assist.spec.ts: Arrow bytes + the page image ride `page.route`; the assist
// call leaves from the zone SERVER and is seeded on the mock annotator.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const KEY = 'fe00cd746463ad2c/0/19';

/** The same empty annotations table assist.spec.ts loads the canvas with. */
const EMPTY_ARROW = Buffer.from(
	'/////5ADAAAQAAAAAAAKABAADgAHAAgACgAAAAAAAAEQAAAAAAAEAAgACAAAAAQACAAAAAQAAAARAAAANAMAAOwCAAC8AgAAkAIAAGQCAAA0AgAABAIAAKgBAAB4AQAASAEAABwBAADwAAAAxAAAAJgAAABsAAAAOAAAAAQAAAAg/f//EAAAABwAAAAAAAADGAAAAAsAAAB1bmNlcnRhaW50eQAAAAAAVv3//wAAAgBQ/f//EAAAABwAAAAAAAADGAAAAAoAAABjb25maWRlbmNlAAAAAAAAhv3//wAAAgCA/f//EAAAABgAAAAAAAAFFAAAAAUAAABncm91cAAAAAAAAAB0/f//qP3//xAAAAAYAAAAAAAABRQAAAAGAAAAc291cmNlAAAAAAAAnP3//9D9//8QAAAAGAAAAAAAAAUUAAAABgAAAHN0YXR1cwAAAAAAAMT9///4/f//EAAAABgAAAAAAAAFFAAAAAQAAAB0ZXh0AAAAAAAAAADs/f//IP7//xAAAAAYAAAAAAAABRQAAAAFAAAAbGFiZWwAAAAAAAAAFP7//0j+//8QAAAAGAAAAAAAAAMUAAAABQAAAHRfZW5kAAAAAAAAAHr+//8AAAIAdP7//xAAAAAYAAAAAAAAAxQAAAAHAAAAdF9zdGFydAAAAAAApv7//wAAAgCg/v//EAAAABgAAAAAAAAMRAAAAAcAAABwb2x5Z29uAAEAAAAEAAAAyP7//xAAAAAYAAAAAAAAAxQAAAAEAAAAaXRlbQAAAAAAAAAA+v7//wAAAgDE/v//+P7//xAAAAAYAAAAAAAAAxQAAAAGAAAAaGVpZ2h0AAAAAAAAKv///wAAAgAk////EAAAABgAAAAAAAADFAAAAAUAAAB3aWR0aAAAAAAAAABW////AAACAFD///8QAAAAFAAAAAAAAAMQAAAAAQAAAHkAAAAAAAAAfv///wAAAgB4////EAAAABQAAAAAAAADEAAAAAEAAAB4AAAAAAAAAKb///8AAAIAoP///xAAAAAcAAAAAAAABRgAAAAKAAAAc2hhcGVfdHlwZQAAAAAAAJj////M////EAAAABgAAAAAAAADHAAAAAYAAABudW1iZXIAAAAAAAAAAAYACAAGAAYAAAAAAAIAEAAUAAQAAAAPABAAAAAIABAAAAAQAAAAFAAAAAAAAAUUAAAAAgAAAGlkAAAAAAAABAAEAAQAAAD/////AAAAAA==',
	'base64',
);

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
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

test('the brush exposes radius and output — and the output toggle actually flips', async ({
	page,
}) => {
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('annotator-toolbar')).toBeVisible();

	// No brush selected ⇒ no brush controls. The controls belong to the tool, not the bar.
	await expect(page.getByTestId('brush-controls')).not.toBeVisible();

	// `9` is the brush hotkey; the button needs the engine attached, the key does not wait on it.
	await page.getByTitle(/^Brush/).click();

	await expect(page.getByTestId('brush-controls')).toBeVisible();
	// The default radius, printed beside the slider — the number an annotator tunes from.
	await expect(page.getByTestId('brush-controls')).toContainText('20');

	const output = page.getByTestId('brush-output');
	await expect(output).toHaveText(/mask/i);
	await output.click();
	await expect(output).toHaveText(/poly/i);
	await output.click();
	await expect(output).toHaveText(/mask/i);
});

test('image adjustments open from the toolbar, and Reset only means something once adjusted', async ({
	page,
}) => {
	await page.goto(`/annotator/?keys=${KEY}`);
	await expect(page.getByTestId('annotator-toolbar')).toBeVisible();

	await page.getByTestId('image-adjust-trigger').click();
	const panel = page.getByTestId('image-adjust');
	await expect(panel).toBeVisible();

	// Neutral on open: all three axes at 1.00, nothing to reset.
	await expect(panel.getByText('1.00')).toHaveCount(3);
	const reset = page.getByTestId('image-adjust-reset');
	await expect(reset).toBeDisabled();

	// Nudge brightness off neutral (a click on the track jumps the thumb).
	await panel.getByLabel('Brightness').click({ position: { x: 100, y: 5 } });
	await expect(reset).toBeEnabled();
	await expect(panel.getByText('1.00')).not.toHaveCount(3);

	await reset.click();
	await expect(panel.getByText('1.00')).toHaveCount(3);
	await expect(reset).toBeDisabled();
});

test('a Detect prediction lands RANKED — its uncertainty rides the wire onto the sidebar', async ({
	page,
}) => {
	// The runner contract's answer, scores included — exactly what the service's own mock now emits.
	// For a while the client dropped both scores on insert, so every prediction rendered scoreless
	// and the review queue's "highest uncertainty first" order had nothing to rank.
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /api/assist/producers': {
					producers: [
						{ name: 'grounding-dino', configured: true, returns: ['bbox'], compatible: null },
					],
					default_configured: false,
					urls_redacted: true,
				},
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
							uncertainty: 0.45,
						},
					],
					source: 'model:grounding-dino',
				},
			},
		},
	});

	await page.goto(`/annotator/?keys=${KEY}`);
	await page.getByPlaceholder(/AI detect/).fill('text');
	await page.getByRole('button', { name: 'Run', exact: true }).click();

	// The badge the list renders per ranked row — visible without opening the detail pane.
	await expect(page.getByTitle('uncertainty')).toHaveText('0.45');

	// And the detail pane shows both scores once the row is selected.
	await page.getByTestId('annotation-list').getByRole('button').last().click();
	const detail = page.getByTestId('annotation-detail');
	await expect(detail).toContainText('0.75');
	await expect(detail).toContainText('0.45');
});
