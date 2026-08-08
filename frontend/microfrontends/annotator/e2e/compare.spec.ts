import { test, expect, type Route } from '@playwright/test';
import { tableToIPC, tableFromArrays } from 'apache-arrow';
import { MOCK_ANNOTATOR } from './ports';

// THE COMPARE VIEW — the pairwise/preference question over a multi-key item. The canvas walks
// candidates one at a time; `?view=compare` lays them out side by side and puts the ontology's
// choices under each, and a pick is an ordinary item-level `tag` row saved onto the WINNING
// unit's own annotations (version-guarded — `base_version` is the version the pane was read at).
// The spec pins the contract at the wire: what the pick POSTs, and that the two views are the
// same item reached from one another.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DOC = 'fe00cd746463ad2c';
const KEY_A = `${DOC}/0/19`;
const KEY_B = `${DOC}/0/20`;
const PNG = Buffer.from(
	'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
	'base64',
);

const VERSION = '3';

function ipc(tags: string[]): Buffer {
	const n = tags.length || 1;
	const table = tableFromArrays({
		id: tags.length ? tags.map((_, i) => `t${i}`) : ['b1'],
		shape_type: tags.length ? tags.map(() => 'tag') : ['bbox'],
		label: tags.length ? tags : ['stamp'],
		status: Array.from({ length: n }, () => 'accepted'),
		source: Array.from({ length: n }, () => 'human'),
	});
	return Buffer.from(tableToIPC(table, 'stream'));
}

const TASK = {
	task_id: 't1',
	project_id: 'p1',
	state: 'claimed',
	assignee: 'gina',
	source: { kind: 'chunk', keys: [KEY_A, KEY_B] },
	media: { kind: 'image' },
	ontology: {
		kind: 'image-classification',
		modality: 'image',
		classes: [
			{ name: 'preferred', colour: null, tools: ['tag'], attributes: [], required: false },
			{ name: 'damaged', colour: null, tools: ['tag'], attributes: [], required: false },
		],
		relations: [],
		allow_empty: false,
	},
};

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

test('a pick POSTs one version-guarded tag insert onto the WINNING unit', async ({ page }) => {
	// Pane B's saved state flips once the pick lands — the reload after save must SHOW the pick.
	let savedOnB: string[] = [];
	const posts: { path: string; body: unknown }[] = [];
	await page.route('**/annotator/api/annotations/**', async (route) => {
		const url = new URL(route.request().url());
		if (route.request().method() === 'POST') {
			posts.push({ path: url.pathname, body: route.request().postDataJSON() });
			if (url.pathname.endsWith('/20')) savedOnB = ['preferred'];
			return json(route, { status: 'ok' });
		}
		if (url.pathname.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': VERSION },
			body: ipc(url.pathname.endsWith('/20') ? savedOnB : []),
		});
	});

	await page.goto(
		`/annotator/?keys=${encodeURIComponent(`${KEY_A},${KEY_B}`)}&task=t1&view=compare`,
	);
	const view = page.getByTestId('compare-view');
	await expect(view).toBeVisible();
	await expect(view.getByTestId('compare-pane-A')).toBeVisible();
	await expect(view.getByTestId('compare-pane-B')).toBeVisible();

	await view.getByTestId('prefer-B-preferred').click();

	// The wire: ONE insert, the right label, pinned to the version the pane was read at.
	await expect(view.getByTestId('prefer-B-preferred')).toHaveAttribute('aria-pressed', 'true');
	expect(posts).toHaveLength(1);
	expect(posts[0]!.path.endsWith(`/annotations/${KEY_B}`)).toBe(true);
	const body = posts[0]!.body as {
		inserts: { shape_type: string; label: string }[];
		deletes: string[];
		base_version: number;
	};
	expect(body.inserts).toHaveLength(1);
	expect(body.inserts[0]).toMatchObject({ shape_type: 'tag', label: 'preferred' });
	expect(body.deletes).toEqual([]);
	expect(body.base_version).toBe(Number(VERSION));

	// The judgment is visible as pane state, and the loser carries nothing.
	await expect(view.getByTestId('compare-pane-B').getByTestId('pane-applied')).toHaveText(
		'1 applied',
	);
	await expect(view.getByTestId('compare-pane-A').getByTestId('pane-applied')).toHaveCount(0);
});

test('the compare view and the canvas are the SAME item, reached from one another', async ({
	page,
}) => {
	await page.route('**/annotator/api/annotations/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (route.request().method() !== 'GET') return json(route, { detail: 'unstubbed write' }, 404);
		if (path.endsWith('/versions')) return json(route, { versions: [] });
		return route.fulfill({
			status: 200,
			contentType: 'application/vnd.apache.arrow.stream',
			headers: { 'X-Annotations-Version': VERSION },
			body: ipc([]),
		});
	});

	// The canvas offers compare only because the item HAS candidates (2 keys).
	await page.goto(`/annotator/?keys=${encodeURIComponent(`${KEY_A},${KEY_B}`)}&task=t1`);
	await page.getByTestId('enter-compare').click();
	await expect(page.getByTestId('compare-view')).toBeVisible();

	// And back — the URL carries the mode, so both directions are plain links.
	await page.getByTestId('exit-compare').click();
	await expect(page.getByTestId('compare-view')).toHaveCount(0);
	await expect(page.getByTestId('page-nav')).toBeVisible();
});
