import { test, expect, type Page, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// REOPENING A TASK KEEPS ITS RELATIONS. Links ride the task draft, and the draft sync posts the
// controller's links on every save — so before the restore, a reopened canvas (links = []) wiped
// every drawn relation on its first save. This spec pins the full loop at the wire: the draft's
// links render in the inspector at open, and the NEXT draft save still carries them.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY = `${DOC}/0/19`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const LINK = { name: 'answers', from_shape: 'q1', to_shape: 'v1' };

const TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY] },
	media: { kind: 'image' },
	ontology: {
		kind: 'document-question-answering',
		modality: 'image',
		classes: [
			{ name: 'question', colour: null, tools: ['bbox'], attributes: [], required: false },
			{ name: 'value', colour: null, tools: ['bbox'], attributes: [], required: false },
		],
		relations: [
			{
				name: 'answers',
				from_classes: ['question'],
				to_classes: ['value'],
				directed: true,
				required: false,
			},
		],
		allow_empty: false,
	},
};

const DRAFT = {
	task_id: 't1',
	project_id: 'p1',
	author: 'gina',
	shapes: [],
	links: [LINK],
	revision: 3,
	origin: 'human',
};

function ipc(): Buffer {
	const table = tableFromArrays({
		id: ['q1', 'v1'],
		shape_type: ['bbox', 'bbox'],
		x: Float32Array.from([40, 300]),
		y: Float32Array.from([40, 200]),
		width: Float32Array.from([180, 90]),
		height: Float32Array.from([120, 60]),
		label: ['question', 'value'],
		status: ['accepted', 'accepted'],
		source: ['human', 'human'],
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

const calls = async (page: Page): Promise<{ method: string; path: string; body: unknown }[]> => {
	const res = await page.request.get(`${MOCK_ANNOTATOR}/__mock/calls`);
	return (await res.json()).calls as { method: string; path: string; body: unknown }[];
};

test('a reopened task SHOWS its links, and the next draft save still CARRIES them', async ({
	page,
}) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, {
		data: {
			routes: {
				'GET /tasks/t1': TASK,
				'GET /tasks/t1/draft': DRAFT,
				'PUT /tasks/t1/draft': { ...DRAFT, revision: 4 },
			},
		},
	});
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
	await page.route('**/annotator/api/chunk-frame/**', (route) =>
		route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
	);
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() === 'POST') return json(route, { saved: 1, version: 2 });
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': '1' },
			body: ipc(),
		});
	});

	await page.goto(`/annotator/?keys=${encodeURIComponent(KEY)}&task=t1`);

	// The restored link is VISIBLE: open the question row — its inspector lists `answers → v1`.
	await page
		.getByTestId('annotation-list')
		.getByRole('button')
		.filter({ hasText: 'question' })
		.click();
	const panel = page.getByTestId('relations-panel');
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('answers');
	await expect(panel).toContainText('→ v1');

	// Make an unrelated edit and save — the draft sync must post the RESTORED link, not [].
	await page.getByText('Group', { exact: true }).locator('..').getByRole('textbox').fill('kie');
	await page.keyboard.press('Control+s');

	await expect
		.poll(
			async () =>
				(await calls(page)).filter((c) => c.method === 'PUT' && c.path.endsWith('/tasks/t1/draft'))
					.length,
			{ timeout: 15_000 },
		)
		.toBeGreaterThan(0);
	const put = (await calls(page)).find(
		(c) => c.method === 'PUT' && c.path.endsWith('/tasks/t1/draft'),
	)!;
	expect((put.body as { links: unknown[] }).links).toEqual([LINK]);
});
