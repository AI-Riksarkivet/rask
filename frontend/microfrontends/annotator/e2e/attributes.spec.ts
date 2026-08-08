import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// PER-ROW ATTRIBUTES — the ontology's typed per-class fields (reading order, script, damaged…),
// edited in the inspector and carried as the row's `metadata` JSON. This spec pins the two ends
// the unit tests cannot: the editor renders FROM THE ONTOLOGY (per the selected row's class), and
// the value goes out on the ordinary save wire as an EDITABLE_FIELDS patch — `{id, metadata}` in
// `edits`, not a parallel store.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: 'image' },
	ontology: {
		kind: 'reading-order',
		modality: 'image',
		classes: [
			{
				name: 'paragraph',
				colour: null,
				tools: ['bbox'],
				attributes: [
					{ name: 'order', type: 'int', choices: [], required: true },
					{ name: 'script', type: 'enum', choices: ['blackletter', 'cursive'], required: false },
				],
				required: false,
			},
			{ name: 'stamp', colour: null, tools: ['bbox'], attributes: [], required: false },
		],
		relations: [],
		allow_empty: false,
	},
};

function ipc(): Buffer {
	const table = tableFromArrays({
		id: ['par', 'st'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([40, 300]),
		y: Float32Array.from([40, 200]),
		width: Float32Array.from([180, 90]),
		height: Float32Array.from([120, 60]),
		label: ['paragraph', 'stamp'],
		status: ['accepted', 'accepted'],
		source: ['human', 'human'],
		metadata: ['{}', '{}'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: { routes: { 'GET /tasks/t1': TASK } },
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
});

test('the editor renders from the ONTOLOGY per class, and the value rides the save wire', async ({
	page,
}) => {
	const saves: { edits: { id: string; metadata?: string }[] }[] = [];
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() === 'POST') {
			saves.push(route.request().postDataJSON() as (typeof saves)[number]);
			return json(route, { saved: 1, version: 2 });
		}
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: ipc(),
		});
	});

	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&task=t1`);

	// Select the paragraph — its class declares attributes, so the panel appears; `order` is
	// marked required (the submit validator refuses a paragraph without it).
	await page
		.getByTestId('annotation-list')
		.getByRole('button')
		.filter({ hasText: 'paragraph' })
		.click();
	const panel = page.getByTestId('attributes-panel');
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('order');
	await expect(panel).toContainText('*');

	await panel.getByTestId('attr-order').fill('2');
	await panel.getByTestId('attr-script').selectOption('cursive');

	// Save flushes ONE metadata patch on the ordinary edits wire.
	await page.keyboard.press('Control+s');
	await expect.poll(() => saves.length, { timeout: 10_000 }).toBeGreaterThan(0);
	const edit = saves[0]!.edits.find((e) => e.id === 'par');
	expect(edit).toBeTruthy();
	expect(JSON.parse(edit!.metadata!)).toEqual({ order: '2', script: 'cursive' });

	// The stamp declares NO attributes — no panel, not an empty form. (The save's reload has
	// already returned the sidebar to the list.)
	await page
		.getByTestId('annotation-list')
		.getByRole('button')
		.filter({ hasText: 'stamp' })
		.click({ timeout: 15_000 });
	await expect(page.getByTestId('annotation-detail')).toBeVisible();
	await expect(page.getByTestId('attributes-panel')).toHaveCount(0);
});
