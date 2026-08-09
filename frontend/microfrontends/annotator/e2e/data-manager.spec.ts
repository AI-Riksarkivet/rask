import { test, expect, type Page, type Route } from '@playwright/test';
import { MOCK_ANNOTATOR } from './ports';

// The queue as a DATA MANAGER — grid mode, item columns, select-all-matching, quick view.
//
// The unit suites pin the pure rules (which bulk actions a selection can take, when to offer the
// rest of a filter, how a tile URL is built, where prev/next stop). What only a BROWSER can prove is
// that they compose: that the grid and the table are two views of ONE selection, that a filter
// narrows both, and that ticking twenty rows then accepting the offer really does hold every match
// across pages.
//
// Hermetic like the rest of this suite: every read is a remote function running on the ZONE SERVER,
// which `page.route` cannot see, so the wire is seeded on the mock annotator.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, { data: { routes } });
};

const CLAIMABLE = [{ event: 'claim', to: 'claimed', permission: 'can_claim' }];

/** One task, with the ITEM columns the queue now renders: a corpus, a modality, and the prediction a
 *  bulk label attached. */
function task(n: number, over: Record<string, unknown> = {}) {
	return {
		task_id: `t${n}`,
		project_id: 'p1',
		state: 'unassigned',
		assignee: null,
		lease_expires_at: null,
		source: { kind: 'chunks', keys: [`vol-${n}/1/${n}`], where: 'vasa' },
		media: { kind: 'image', image_url: null },
		prediction: [{ shape_type: 'tag', label: n % 2 ? 'letter' : 'envelope', source: 'bulk' }],
		review_required: true,
		submitted_by: null,
		reviewed_by: null,
		review_action: null,
		review_notes: [],
		legal_events: CLAIMABLE,
		...over,
	};
}

function listing(details: Record<string, unknown>[]) {
	return {
		tasks: Object.fromEntries(details.map((d) => [d['task_id'], d['state']])),
		counts: { unassigned: details.length },
		total: details.length,
		terminal: 0,
		may_publish: false,
		details,
		missing: [],
	};
}

const project = {
	project_id: 'p1',
	tenant: 'default',
	slug: 'vasa',
	title: 'Vasa letters',
	state: 'labeling',
	review_required: true,
	lease_seconds: 1800,
	consensus_n: 1,
	counts: {},
	published: null,
	publish_error: null,
	publish_progress: null,
	pending_target_namespace: null,
};

/** 25 items, so the queue spans more than one page (pageSize 20) — the select-all offer only has
 *  something to offer when the filter matches more than a page. */
const MANY = Array.from({ length: 25 }, (_, i) => task(i + 1));

async function openQueue(page: Page, details = MANY): Promise<void> {
	await seed(page, {
		'GET /projects/p1': { project, legal_events: [] },
		'GET /projects/p1/tasks?include=details': listing(details),
		'GET /projects/p1/members': { members: [], total: 0 },
	});
	await page.goto('/annotator/tasks/p1');
	await expect(page.getByTestId('queue-view-toggle')).toBeVisible();
}

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	// The frame endpoint the tiles request. 404 on purpose: a tile whose image fails must still
	// render as a tile, which is the `onerror` fallback doing its job.
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
});

// --------------------------------------------------------------------------------------------------
// The item's own columns
// --------------------------------------------------------------------------------------------------

test('the queue shows the ITEM columns, not only task administration', async ({ page }) => {
	// It had five columns and every one was admin — key, state, lease, provenance, actions. Nothing
	// described the thing being labelled, so "which of these did the machine call letter" had no
	// answer in the table.
	await openQueue(page);

	const head = page.locator('table thead');
	for (const column of ['item', 'label', 'corpus', 'media', 'state']) {
		await expect(head.getByText(column, { exact: true })).toBeVisible();
	}
	// The label a bulk send attached — invisible until `prediction` was declared in the wire schema,
	// because valibot strips what it does not declare, in silence.
	await expect(page.locator('table tbody').getByText('letter').first()).toBeVisible();
});

test('a free-text filter narrows the queue, and says by how much', async ({ page }) => {
	await openQueue(page);

	await page.getByTestId('filter-text').fill('vol-13');

	await expect(page.getByTestId('filter-count')).toHaveText(/1 of 25/);
});

// --------------------------------------------------------------------------------------------------
// Grid mode
// --------------------------------------------------------------------------------------------------

test('grid mode renders the table’s current PAGE, not the whole queue', async ({ page }) => {
	// Rendering the unpaginated set would turn a thousand-item queue into a thousand tiles.
	await openQueue(page);

	await page.getByTestId('view-grid').click();

	await expect(page.getByTestId('task-grid')).toBeVisible();
	await expect(page.getByTestId('task-tile')).toHaveCount(20);
	// The queue's table is REPLACED, not hidden beside it — two live tables would mean two sources
	// of truth about what is in view.
	await expect(page.locator('table')).toHaveCount(0);
});

test('a tile with no loadable image is still a TILE', async ({ page }) => {
	// The frame endpoint 404s in this suite. A tile that vanished on a failed image would make a
	// whole grid disappear the moment the viewer was down.
	await openQueue(page);

	await page.getByTestId('view-grid').click();

	await expect(page.getByTestId('task-tile').first()).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// ONE selection, two views
// --------------------------------------------------------------------------------------------------

test('a selection made in the GRID is the same selection in the LIST', async ({ page }) => {
	// The property that makes the grid a second VIEW rather than a second, weaker table. Every bulk
	// action reads one object, so neither mode needs its own code path.
	await openQueue(page);
	await page.getByTestId('view-grid').click();

	// `exact` matters: without it "Select 1/1" also matches "Select 1/10" … "Select 1/19", and
	// Playwright's strict mode refuses the ambiguity.
	await page.getByRole('button', { name: 'Select 1/1', exact: true }).click();
	await page.getByRole('button', { name: 'Select 1/2', exact: true }).click();
	await expect(page.getByTestId('bulk-bar')).toContainText('2 selected');

	await page.getByTestId('view-list').click();

	await expect(page.getByTestId('task-grid')).toHaveCount(0);
	await expect(page.getByTestId('bulk-bar')).toContainText('2 selected');
});

test('the bulk vocabulary is DERIVED from the rows, with honest counts', async ({ page }) => {
	// It used to be two hardcoded buttons (accept, assign) while every per-row button came from the
	// task machine — so claiming twenty items was twenty clicks.
	await openQueue(page, [
		task(1),
		task(2, {
			state: 'claimed',
			legal_events: [{ event: 'release', to: 'unassigned', permission: 'can_annotate' }],
		}),
	]);

	await page.locator('table thead button[role=checkbox]').click();

	const bar = page.getByTestId('bulk-bar');
	await expect(bar).toContainText('2 selected');
	// One row can be claimed and one released — each action names how many it will actually touch,
	// rather than acting on a subset silently.
	await expect(bar.getByTestId('bulk-claim')).toContainText('1');
	await expect(bar.getByTestId('bulk-release')).toContainText('1');
});

// --------------------------------------------------------------------------------------------------
// Select all N matching
// --------------------------------------------------------------------------------------------------

test('selecting the page OFFERS the rest of the filter, and holds it across pages', async ({
	page,
}) => {
	// The header checkbox is `toggleAllPageRowsSelected` — the visible page and nothing else. So
	// filtering to 25 and acting on all of them meant ticking 20, acting, paging, ticking 5.
	await openQueue(page);

	await page.locator('table thead button[role=checkbox]').click();
	await expect(page.getByTestId('bulk-bar')).toContainText('20 selected');

	const offer = page.getByTestId('select-all-offer');
	await expect(offer).toContainText('All 20 on this page');
	await offer.getByTestId('select-all-matching').click();

	await expect(page.getByTestId('bulk-bar')).toContainText('25 selected');
	// And it SAYS it is complete, rather than continuing to offer a selection already held — which
	// would read as the button not having worked.
	await expect(page.getByTestId('select-all-held')).toContainText('All 25');
	await expect(page.getByTestId('select-all-offer')).toHaveCount(0);
});

test('the offer stays SILENT while the page is only partly selected', async ({ page }) => {
	// The person is still choosing. Offering "select all 25" mid-choice is noise at best and a
	// misclick at worst.
	await openQueue(page);

	await page.locator('table tbody button[role=checkbox]').first().click();

	await expect(page.getByTestId('bulk-bar')).toContainText('1 selected');
	await expect(page.getByTestId('select-all-offer')).toHaveCount(0);
});

// --------------------------------------------------------------------------------------------------
// Quick view
// --------------------------------------------------------------------------------------------------

test('quick view reviews an item BESIDE the queue, and walks the page', async ({ page }) => {
	// Reviewing through the canvas route costs the filter, the page and the selection on every round
	// trip — fifty-seven items is fifty-seven of them.
	await openQueue(page);

	await page.getByTestId('row-quick-view').first().click();

	// `getByTestId`, not `getByRole('dialog')`: quick view is a PANE beside the queue now, not an
	// overlay, so it is deliberately not a dialog — nothing is modal and the table stays interactive
	// next to it. Announcing it as a dialog would tell a screen-reader user focus is trapped when it
	// is not. The assertions below are unchanged.
	const pane = page.getByTestId('quick-view');
	await expect(pane).toContainText('1 of 20 in view');
	await expect(pane).toContainText('suggested');

	// The queue is still THERE and still usable — the property a split has and an overlay does not,
	// and the reason this test is named "BESIDE the queue".
	await expect(page.getByTestId('row-quick-view').first()).toBeVisible();
	// It does NOT wrap: a review pass that loops from the end back to the start never tells you that
	// you have seen everything.
	await expect(pane.getByTestId('quick-prev')).toBeDisabled();

	await pane.getByTestId('quick-next').click();

	await expect(pane).toContainText('2 of 20 in view');
	await expect(pane.getByTestId('quick-prev')).toBeEnabled();
});

test('quick view is scoped to what the FILTER matched', async ({ page }) => {
	// Reviewing in the order you are looking at things, rather than whatever the server returned.
	await openQueue(page);
	await page.getByTestId('filter-text').fill('vol-1/');

	await page.getByTestId('row-quick-view').first().click();

	await expect(page.getByTestId('quick-view')).toContainText('1 of 1 in view');
});
