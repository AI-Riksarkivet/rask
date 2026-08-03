import { test, expect, type Route } from '@playwright/test';

// The send half of the annotation funnel, hermetic: a selection found HERE (search results;
// the atlas path shares the same dialog) is sent into an annotation project as claimable
// tasks — appended to a project still taking items, or a new project created around it.
// The annotator zone's own suite covers the other end (claim → review → publish).

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const HEALTH = {
	db: { path: '/data/demo.lance', tables: ['chunks'], chunks: 2, documents: 1 },
	embed: { ok: true, url: 'http://embed', error: null },
	rerank: { ok: true, url: 'http://rerank', error: null },
};
const col = (name: string, arrow_type: string) => ({
	name,
	arrow_type,
	nullable: true,
	vector_dim: null,
	is_blob: false,
});
const DESCRIPTOR = {
	id: 'demo',
	tables: {
		chunks: {
			name: 'chunks',
			row_count: 2,
			version: 1,
			columns: [
				col('doc_id', 'string'),
				col('speech_id', 'int64'),
				col('chunk_id', 'int64'),
				col('title', 'string'),
				col('text', 'string'),
			],
			indexes: [],
		},
	},
	declared: {
		identity: { key_fields: ['doc_id', 'speech_id', 'chunk_id'], doc_key: 'doc_id' },
		display: { title: ['title'], body: 'text' },
		search: { row_table: 'chunks' },
	},
	capabilities: [],
};
const HIT = {
	doc_id: 'd1',
	speech_id: 0,
	chunk_id: 0,
	title: 'Hello world',
	text: 'the quick brown fox jumps',
	_score: 3.2,
	alignments: [],
};

const PROJECTS = {
	projects: [
		{ project_id: 'p1', slug: 'vasa', title: 'Vasa portraits', state: 'labeling', counts: {} },
		{ project_id: 'p2', slug: 'done', title: 'Closed', state: 'published', counts: {} },
	],
	total: 2,
};

let writes: { path: string; body: unknown }[] = [];

test.beforeEach(async ({ page }) => {
	writes = [];
	await page.route('**/media/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/media/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
	await page.route('**/media/api/health', (route) => json(route, HEALTH));
	await page.route('**/media/api/datasets/demo/descriptor', (route) => json(route, DESCRIPTOR));
	await page.route('**/media/api/search**', (route) => json(route, [HIT]));
	await page.route('**/media/api/projects?tenant=*', (route) => json(route, PROJECTS));
	await page.route('**/media/api/projects/p1/items', (route) => {
		writes.push({ path: '/media/api/projects/p1/items', body: route.request().postDataJSON() });
		return json(route, { sent: 1, created: 1, task_ids: ['t1'] });
	});
});

async function searchAndOpenDialog(page: import('@playwright/test').Page): Promise<void> {
	await page.goto('/media/');
	const input = page.getByPlaceholder(/Search transcripts/);
	await input.fill('fox');
	await input.press('Enter');
	await expect(page.getByText('Hello world').first()).toBeVisible();
	await page.getByRole('button', { name: 'Send to labeling task…' }).click();
}

test('search results send into an OPEN project as tasks, with the descriptor key-path', async ({
	page,
}) => {
	await searchAndOpenDialog(page);

	// Only projects still taking items are offered — the published one must not be.
	const list = page.getByTestId('send-project-list');
	await expect(list.getByText('Vasa portraits')).toBeVisible();
	await expect(list.getByText('Closed')).not.toBeVisible();

	await list.getByText('Vasa portraits').click();

	// The send carried the hit's key-path, the dataset — and the dataset's VERSION at send
	// time (the §7.2 reproducibility capture the publish pin rests on; the descriptor says 1).
	expect(writes).toHaveLength(1);
	const body = writes[0]?.body as {
		items: {
			source: { kind: string; keys: string[]; where: string; dataset_version: number | null };
		}[];
	};
	expect(body.items).toHaveLength(1);
	expect(body.items[0]?.source.keys).toEqual(['d1/0/0']);
	expect(body.items[0]?.source.where).toBe('demo');
	expect(body.items[0]?.source.dataset_version).toBe(1);

	// The done state links into the annotator — a cross-zone anchor, hard-navigated.
	const link = page.getByTestId('send-done').getByRole('link', { name: /Open the labeling task/ });
	await expect(link).toHaveAttribute('href', '/annotator/projects/p1');
	await expect(link).toHaveAttribute('data-sveltekit-reload', '');
});

test('create-and-send makes the project, then appends the selection to it', async ({ page }) => {
	await page.route('**/media/api/projects', (route) => {
		if (route.request().method() !== 'POST') return json(route, PROJECTS);
		writes.push({ path: 'POST /media/api/projects', body: route.request().postDataJSON() });
		return json(route, { project_id: 'p9', slug: 'fresh', title: 'Fresh', state: 'draft' });
	});
	await page.route('**/media/api/projects/p9/items', (route) => {
		writes.push({ path: '/media/api/projects/p9/items', body: route.request().postDataJSON() });
		return json(route, { sent: 1, created: 1, task_ids: ['t1'] });
	});

	await searchAndOpenDialog(page);
	await page.getByPlaceholder('slug (vasa-portraits)').fill('fresh');
	await page.getByRole('button', { name: /Create & send/ }).click();

	await expect(page.getByTestId('send-done')).toBeVisible();
	const paths = writes.map((w) => w.path);
	expect(paths).toContain('POST /media/api/projects');
	expect(paths).toContain('/media/api/projects/p9/items');
});

test('a refused send surfaces the server’s reason, not a silent no-op', async ({ page }) => {
	await page.route('**/media/api/projects/p1/items', (route) =>
		json(route, { detail: 'gina lacks can_send_items on annotation_project:p1' }, 403),
	);

	await searchAndOpenDialog(page);
	await page.getByTestId('send-project-list').getByText('Vasa portraits').click();

	await expect(page.getByTestId('send-error')).toContainText('can_send_items');
});
