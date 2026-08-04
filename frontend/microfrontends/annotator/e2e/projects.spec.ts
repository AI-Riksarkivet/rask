import { test, expect, type Page, type Route } from '@playwright/test';
import { MOCK_ANNOTATOR } from './ports';

// Hermetic coverage for the annotation task-management surfaces (OPEN-WORK.md § Design — annotation
// projects; the A1–A4 surfaces). The backend's OWN contracts (FGA doors, machine tables, saga
// idempotency, template enforcement at submit) are pinned by tests/unit/*.
// What THIS layer proves: the UI renders the transitions the backend supplies, drives the right
// endpoints with the right bodies, keeps the three review actions distinct, states what a publish
// lands before firing it, narrates a running publish, and surfaces a server 403 as the refusal it is.
//
// The transport is remote functions now (the transport ruling, area 4): every read and write below runs
// on the zone SERVER, which `page.route` cannot see — so the wire is seeded on, and asserted through,
// the mock annotator's ledger (e2e/mock-annotator.ts). Same assertions as the BFF-era spec; the paths
// are the UPSTREAM ones (`/tasks/t1/events`) rather than the proxied `/annotator/api/…`, because that
// is now where the request actually goes.

type Body = Record<string, unknown>;

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const KEY = 'fe00cd746463ad2c/0/19';

/** Seed exact upstream responses, keyed "METHOD /path" — a key WITHOUT a query string answers any
 *  query, which is the old `?tenant=*` glob's replacement. */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/seed`, { data: { routes } });
};

/** Every mutating request the zone server made, in order. */
const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_ANNOTATOR}/__mock/calls`);
	return ((await res.json()) as { calls: Body[] }).calls;
};

const bodies = async (page: Page): Promise<unknown[]> => (await calls(page)).map((c) => c.body);

/** The create POST, once it has landed — polled, so an assertion never races the request. */
async function createCall(page: Page): Promise<Body> {
	await expect.poll(async () => (await calls(page)).some((c) => c.path === '/projects')).toBe(true);
	return (await calls(page)).find((c) => c.path === '/projects')!;
}

const LEGAL = {
	labeling: [
		{ event: 'freeze', to: 'frozen', permission: 'can_manage' },
		{ event: 'send', to: 'labeling', permission: 'can_send_items' },
	],
	frozen: [
		{ event: 'archive', to: 'archived', permission: 'can_manage' },
		{ event: 'open', to: 'labeling', permission: 'can_manage' },
		{ event: 'publish', to: 'publishing', permission: 'can_publish' },
	],
};

const TASK_EVENTS = {
	unassigned: [{ event: 'claim', to: 'claimed', permission: 'can_claim' }],
	claimed: [
		{ event: 'release', to: 'unassigned', permission: 'can_annotate' },
		{ event: 'save_draft', to: 'claimed', permission: 'can_annotate' },
		{ event: 'skip', to: 'skipped', permission: 'can_annotate' },
		{ event: 'submit', to: 'in_review', permission: 'can_annotate' },
	],
	in_review: [
		{ event: 'accept', to: 'accepted', permission: 'can_review' },
		{ event: 'fix_and_accept', to: 'accepted', permission: 'can_review' },
		{ event: 'request_changes', to: 'changes_requested', permission: 'can_review' },
	],
};

function project(
	state: keyof typeof LEGAL | 'draft' | 'publishing' | 'publish_failed',
	extra: Record<string, unknown> = {},
) {
	return {
		project_id: 'p1',
		tenant: 'default',
		slug: 'vasa-portraits',
		title: 'Vasa portraits',
		description: 'Portrait shapes on the Vasa corpus',
		state,
		review_required: true,
		lease_seconds: 1800,
		counts: {
			unassigned: 1,
			claimed: 0,
			in_review: 1,
			changes_requested: 0,
			accepted: 1,
			skipped: 0,
		},
		published: null,
		publish_error: null,
		publish_progress: null,
		pending_target_namespace: null,
		...extra,
	};
}

function task(
	id: string,
	state: keyof typeof TASK_EVENTS | 'accepted' | 'skipped',
	extra: Record<string, unknown> = {},
) {
	return {
		task_id: id,
		project_id: 'p1',
		state,
		assignee: null,
		lease_expires_at: null,
		source: { kind: 'chunks', keys: [KEY] },
		media: { kind: 'image', image_url: null },
		review_required: true,
		submitted_by: null,
		reviewed_by: null,
		review_action: null,
		review_notes: [],
		legal_events: TASK_EVENTS[state as keyof typeof TASK_EVENTS] ?? [],
		...extra,
	};
}

function listing(details: Record<string, unknown>[], extra: Record<string, unknown> = {}) {
	const states = details.map((d) => d['state'] as string);
	return {
		tasks: Object.fromEntries(details.map((d) => [d['task_id'], d['state']])),
		counts: Object.fromEntries(
			['unassigned', 'claimed', 'in_review', 'changes_requested', 'accepted', 'skipped'].map(
				(s) => [s, states.filter((x) => x === s).length],
			),
		),
		total: details.length,
		terminal: states.filter((s) => s === 'accepted' || s === 'skipped').length,
		may_publish: states.every((s) => s === 'accepted' || s === 'skipped') && states.length > 0,
		details,
		missing: [],
		...extra,
	};
}

/** The detail page's ONE snapshot: the project read and the task listing, seeded together — which is
 *  exactly the invariant the page is built on (a reviewer never sees a queue from one snapshot beside
 *  a publish precondition from another). */
const snapshot = (
	page: Page,
	detail: Record<string, unknown>,
	tasks: Record<string, unknown>,
): Promise<void> =>
	seed(page, {
		'GET /projects/p1': detail,
		'GET /projects/p1/tasks?include=details': tasks,
	});

test.beforeEach(async ({ page }) => {
	await page.request.post(`${MOCK_ANNOTATOR}/__mock/reset`);
	// Identity stays a BFF pass-through (keep-flow), so it is still mocked at the browser boundary.
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	// The media plane still rides `+server.ts` bytes routes; nothing on these pages should reach one.
	// Zone-scoped glob on purpose: a bare **/api/** also matches Vite /@fs module URLs.
	await page.route('**/annotator/api/**', (route) => json(route, { detail: 'unstubbed' }, 404));
});

// --------------------------------------------------------------------------------------------------
// A1 · the landing
// --------------------------------------------------------------------------------------------------

test('A1: the landing lists the tenant’s projects with state and progress', async ({ page }) => {
	await seed(page, { 'GET /projects': { projects: [project('labeling')], total: 1 } });

	await page.goto('/annotator/');

	await expect(page.getByRole('heading', { name: 'Labeling tasks' })).toBeVisible();
	const card = page.getByRole('link', { name: /Vasa portraits/ });
	await expect(card).toBeVisible();
	await expect(card.getByText('labeling')).toBeVisible();
	await expect(card.getByText('1/3 items terminal')).toBeVisible();
});

test('A1: a refused list is a REFUSAL, not an empty state', async ({ page }) => {
	await seed(page, {
		'GET /projects': { status: 403, body: { detail: 'gina lacks member on project:default' } },
	});

	await page.goto('/annotator/');

	await expect(page.getByText("You can't list this tenant's labeling tasks.")).toBeVisible();
	await expect(page.getByText('gina lacks member on project:default')).toBeVisible();
	await expect(page.getByText('No labeling tasks yet.')).not.toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// A2 · claim → annotate → submit
// --------------------------------------------------------------------------------------------------

test('A2: claim takes the lease, Annotate routes into the EXISTING canvas, submit hands off to review', async ({
	page,
}) => {
	// Signed in as `anon` (LIFO: this registration wins over the beforeEach 401) — the lease chip's
	// "yours" reads ME against the assignee, and a signed-out UI honestly can't say "yours".
	await page.route('**/annotator/capi/v1/me', (route) =>
		json(route, { sub: 'anon', name: null, email: null, estate_admin: true, projects: [] }),
	);
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([task('t1', 'unassigned')]),
	);

	await page.goto('/annotator/projects/p1');
	await expect(page.getByRole('heading', { name: /Vasa portraits/ })).toBeVisible();

	// The post-claim world, seeded before the click: the event answers with the moved task and the
	// page's refetch reads the moved listing — the UI never invents a transition itself.
	const claimed = task('t1', 'claimed', {
		assignee: 'anon',
		lease_expires_at: new Date(Date.now() + 600_000).toISOString(),
	});
	await seed(page, { 'POST /tasks/t1/events': claimed });
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([claimed]),
	);

	// Claim: the button comes from the task's OWN legal_events.
	await page.getByRole('button', { name: 'Claim' }).click();
	await expect.poll(() => bodies(page)).toContainEqual({ event: 'claim' });

	// The refetched row is claimed with a live lease — and Annotate routes into the canvas
	// with the task's OWN keys (`?keys=`), not a second viewer. (By title: the estate navbar
	// also carries an "Annotate" zone link.)
	await expect(page.getByText(/yours · \d{2}:\d{2}/)).toBeVisible();
	const annotate = page.getByTitle('open this item on the annotate canvas');
	await expect(annotate).toHaveAttribute('href', new RegExp(`keys=${encodeURIComponent(KEY)}`));

	// Submit for review — the working loop's handoff.
	const submitted = task('t1', 'in_review', { submitted_by: 'anon' });
	await seed(page, { 'POST /tasks/t1/events': submitted });
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([submitted]),
	);
	await page.getByRole('button', { name: 'Submit for review' }).click();
	await expect.poll(() => bodies(page)).toContainEqual({ event: 'submit' });
	await expect(page.getByText('in review')).toBeVisible();
});

test('A2: an expired lease is shown EXPIRED, never as held', async ({ page }) => {
	const stale = task('t1', 'claimed', {
		assignee: 'dave',
		lease_expires_at: new Date(Date.now() - 60_000).toISOString(),
	});
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([stale]),
	);

	await page.goto('/annotator/projects/p1');

	await expect(page.getByText('dave · expired')).toBeVisible();
	await expect(page.getByText(/dave · \d{2}:\d{2}/)).not.toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// A3 · review — three DISTINCT actions
// --------------------------------------------------------------------------------------------------

test('A3: accept, fix & accept and request changes are three distinct actions; the note travels', async ({
	page,
}) => {
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([task('t2', 'in_review', { submitted_by: 'gina' })]),
	);
	await seed(page, {
		'POST /tasks/t2/events': task('t2', 'changes_requested', { submitted_by: 'gina' }),
	});

	await page.goto('/annotator/projects/p1');

	// All three, simultaneously visible, separately actionable — never collapsed.
	await expect(page.getByRole('button', { name: 'Accept', exact: true })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Fix & accept', exact: true })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Request changes…' })).toBeVisible();

	await page.getByRole('button', { name: 'Request changes…' }).click();
	await page.getByPlaceholder(/stamp in the corner/).fill('The stamp in the corner is unlabelled');
	await page.getByRole('button', { name: 'Request changes', exact: true }).click();

	await expect
		.poll(() => bodies(page))
		.toContainEqual({
			event: 'request_changes',
			message: 'The stamp in the corner is unlabelled',
		});
	// The distinct-edges guarantee: nothing here fired accept or fix_and_accept.
	const fired = (await bodies(page)).map((b) => (b as { event: string }).event);
	expect(fired).not.toContain('accept');
	expect(fired).not.toContain('fix_and_accept');
});

// --------------------------------------------------------------------------------------------------
// A4 · publish — confirm states the contract; progress is narrated; failure offers retry
// --------------------------------------------------------------------------------------------------

test('A4: the confirm step states what lands and whose names travel; a running publish narrates; failure offers retry', async ({
	page,
}) => {
	const done = listing([
		task('t1', 'accepted', {
			submitted_by: 'gina',
			reviewed_by: 'carol',
			review_action: 'accepted',
		}),
		task('t2', 'skipped'),
	]);
	await snapshot(page, { project: project('frozen'), legal_events: LEGAL.frozen }, done);

	await page.goto('/annotator/projects/p1');

	await page.getByRole('button', { name: 'Publish…' }).click();
	// The confirm step BEFORE anything runs: counts, sentinel honesty, and the names.
	// (Scoped to the dialog; template line breaks mean a `.*` regex can't span the phrases.)
	const dialog = page.getByRole('dialog');
	await expect(dialog.getByText(/accepted item/)).toBeVisible();
	await expect(dialog.getByText(/sentinel rows/)).toBeVisible();
	await expect(dialog.getByText(/gina/)).toBeVisible();
	await expect(dialog.getByText(/carol/)).toBeVisible();

	// Phase 2, seeded before the click: the event is accepted and the refetch reads a RUNNING publish.
	await seed(page, { 'POST /projects/p1/events': project('publishing') });
	await snapshot(
		page,
		{
			project: project('publishing', {
				publish_progress: 'creating table silver$vasa-portraits_0123456789ab',
			}),
			legal_events: [],
		},
		done,
	);

	await page.getByRole('button', { name: 'Publish to silver' }).click();
	await expect
		.poll(() => bodies(page))
		.toContainEqual({
			event: 'publish',
			target_namespace: 'silver',
		});

	// The RUNNING publish narrates the saga's actual step — not a spinner.
	await expect(page.getByText('creating table silver$vasa-portraits_0123456789ab')).toBeVisible();

	// Phase 3: the saga dies. Nothing below clicks — the page's own 2 s poll carries the transition,
	// which is the property under test.
	await snapshot(
		page,
		{
			project: project('publish_failed', {
				publish_error: 'catalog unreachable: connection refused',
				publish_progress: 'creating table silver$vasa-portraits_0123456789ab',
				pending_target_namespace: 'silver',
			}),
			legal_events: [{ event: 'publish', to: 'publishing', permission: 'can_publish' }],
		},
		done,
	);

	// The failure shows the recorded error, the step it died at, and a retry that restates
	// the pinned target namespace.
	await expect(page.getByText('catalog unreachable: connection refused')).toBeVisible({
		timeout: 10_000,
	});
	await expect(page.getByRole('button', { name: 'Retry publish' })).toBeVisible();
	await expect(page.getByText(/must target/)).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// AuthZ · the 403 path
// --------------------------------------------------------------------------------------------------

test('a server 403 surfaces as the refusal it is — named door, no silent no-op', async ({
	page,
}) => {
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([task('t1', 'unassigned')]),
	);
	await seed(page, {
		'POST /tasks/t1/events': {
			status: 403,
			body: { detail: 'gina lacks can_claim on annotation_project:p1' },
		},
	});

	await page.goto('/annotator/projects/p1');
	await page.getByRole('button', { name: 'Claim' }).click();

	await expect(page.getByText('gina lacks can_claim on annotation_project:p1')).toBeVisible();
	// And the row did NOT pretend the claim happened.
	await expect(page.getByRole('button', { name: 'Claim' })).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// Bulk review — the LLM-as-judge foundation
// --------------------------------------------------------------------------------------------------

test('bulk accept fires one gated event per selected reviewable task', async ({ page }) => {
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([
			task('t1', 'in_review', { submitted_by: 'gina' }),
			task('t2', 'in_review', { submitted_by: 'gina' }),
			task('t3', 'unassigned'),
		]),
	);
	await seed(page, {
		'POST /tasks/t1/events': task('t1', 'accepted', {
			submitted_by: 'gina',
			reviewed_by: 'carol',
		}),
		'POST /tasks/t2/events': task('t2', 'accepted', {
			submitted_by: 'gina',
			reviewed_by: 'carol',
		}),
	});

	await page.goto('/annotator/projects/p1');
	await page.getByRole('checkbox', { name: 'Select all' }).check();
	await expect(page.getByTestId('bulk-bar')).toContainText('3 selected · 2 reviewable');
	await page.getByRole('button', { name: 'Accept 2 reviewed' }).click();
	await expect(page.getByText('Accepted 2 items.')).toBeVisible();

	// Exactly the two in_review tasks were accepted — the unassigned one was never fired at.
	const accepted = (await calls(page)).filter(
		(c) => (c.body as { event: string }).event === 'accept',
	);
	expect(accepted.map((c) => c.path).sort()).toEqual(['/tasks/t1/events', '/tasks/t2/events']);
});

// --------------------------------------------------------------------------------------------------
// Assignment — the manager's distribution edge
// --------------------------------------------------------------------------------------------------

test('assign names a recipient and the row comes back pinned', async ({ page }) => {
	const assignable = {
		...task('t1', 'unassigned'),
		legal_events: [
			{ event: 'claim', to: 'claimed', permission: 'can_claim' },
			{ event: 'assign', to: 'claimed', permission: 'can_manage' },
		],
	};
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([assignable]),
	);

	await page.goto('/annotator/projects/p1');
	await page.getByRole('button', { name: 'Assign…' }).click();
	await page.getByPlaceholder(/annotator \(OIDC subject/).fill('dave');

	// The server pins an assigned item: claimed, named assignee, NO lease expiry (§5.2).
	const pinned = task('t1', 'claimed', { assignee: 'dave', lease_expires_at: null });
	await seed(page, { 'POST /tasks/t1/events': pinned });
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([pinned]),
	);
	await page.getByRole('button', { name: 'Assign', exact: true }).click();

	await expect.poll(() => bodies(page)).toContainEqual({ event: 'assign', assignee: 'dave' });
	// The pinned chip: held by dave, no countdown — an assignment never expires.
	await expect(page.getByText('dave · pinned')).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// Consensus v1 — replica items (B)
// --------------------------------------------------------------------------------------------------

test('consensus: the create dialog carries the field and the create POST carries consensus_n', async ({
	page,
}) => {
	await seed(page, {
		'GET /projects': { projects: [], total: 0 },
		'POST /projects': project('draft', { consensus_n: 3 }),
	});

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();

	const dialog = page.getByRole('dialog');
	await expect(dialog.getByText(/annotators per item/)).toBeVisible();
	await dialog.getByPlaceholder('vasa-portraits').fill('vasa-portraits');
	await dialog.getByRole('spinbutton').fill('3');
	// Assert the binding landed BEFORE submitting: `fill` returns once the input event is dispatched,
	// not once Svelte has re-run the binding, and an Enter that beats it submits the default value —
	// which reads as "the dialog dropped the field" rather than as the race it is.
	await expect(dialog.getByRole('spinbutton')).toHaveValue('3');
	await dialog.getByPlaceholder('vasa-portraits').press('Enter');

	const create = await createCall(page);
	expect((create.body as { consensus_n: number }).consensus_n).toBe(3);
});

test('consensus: replica items wear a replica k/N chip from their deterministic ids', async ({
	page,
}) => {
	await snapshot(
		page,
		{ project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling },
		listing([
			task('g1-r1', 'unassigned', { replica_of: 'g1' }),
			task('g1-r2', 'claimed', { replica_of: 'g1', assignee: 'dave' }),
			task('t9', 'unassigned'), // an ordinary item — no chip
		]),
	);

	await page.goto('/annotator/projects/p1');

	await expect(page.getByText('replica 1/2')).toBeVisible();
	await expect(page.getByText('replica 2/2')).toBeVisible();
	// Exactly the two replicas carry chips — the ordinary item stays chipless.
	await expect(page.getByText(/replica \d\/\d/)).toHaveCount(2);
});

test('consensus: the one-replica-per-annotator 409 surfaces verbatim, and the row does not move', async ({
	page,
}) => {
	await snapshot(
		page,
		{ project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling },
		listing([task('g1-r2', 'unassigned', { replica_of: 'g1' })]),
	);
	await seed(page, {
		'POST /tasks/g1-r2/events': {
			status: 409,
			body: {
				detail:
					'one replica per annotator per group: gina already holds or worked replica g1-r1 of group g1',
			},
		},
	});

	await page.goto('/annotator/projects/p1');
	await page.getByRole('button', { name: 'Claim' }).click();

	await expect(
		page.getByText(
			/one replica per annotator per group: gina already holds or worked replica g1-r1/,
		),
	).toBeVisible();
	// The refusal did not fake a claim.
	await expect(page.getByRole('button', { name: 'Claim' })).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// Consensus v1 — adjudication (the manager's pick) and the instructions surface
// --------------------------------------------------------------------------------------------------

test('adjudication: the manager picks a canonical replica; the pick PUTs and the chips mark it', async ({
	page,
}) => {
	const replicas = listing([
		task('g1-r1', 'accepted', { replica_of: 'g1', submitted_by: 'gina' }),
		task('g1-r2', 'accepted', { replica_of: 'g1', submitted_by: 'dave' }),
	]);
	const detail = (adjudications: Record<string, unknown>) => ({
		project: project('labeling', { consensus_n: 2, adjudications }),
		legal_events: LEGAL.labeling,
	});
	await snapshot(page, detail({}), replicas);

	await page.goto('/annotator/projects/p1');

	const panel = page.getByTestId('adjudication-panel');
	await expect(panel.getByText('Adjudication')).toBeVisible();
	await expect(panel.getByText('gina')).toBeVisible();
	await expect(panel.getByText('dave')).toBeVisible();

	// The picked world, seeded before the click: the PUT records the pick and the refetched project
	// carries it — the UI never marks a replica canonical on its own.
	const picked = { g1: { task_id: 'g1-r1', by: 'anon', at: new Date().toISOString() } };
	await seed(page, {
		'PUT /projects/p1/adjudications/g1': project('labeling', {
			consensus_n: 2,
			adjudications: picked,
		}),
	});
	await snapshot(page, detail(picked), replicas);

	await panel.getByRole('button', { name: 'Pick', exact: true }).first().click();

	await expect.poll(() => bodies(page)).toContainEqual({ task_id: 'g1-r1' });
	// The refetched pick marks the replica in BOTH surfaces: the panel and the queue row.
	await expect(panel.getByText('canonical', { exact: true })).toBeVisible();
	await expect(page.getByRole('table').getByText('canonical')).toBeVisible();
	// The runner-up stays re-pickable — a pick is pre-publish metadata, not a ratchet.
	await expect(panel.getByRole('button', { name: 'Re-pick' })).toBeVisible();

	// Withdraw (the un-wedge path): DELETE clears the pick and the chips go with it.
	await seed(page, {
		'DELETE /projects/p1/adjudications/g1': project('labeling', {
			consensus_n: 2,
			adjudications: {},
		}),
	});
	await snapshot(page, detail({}), replicas);
	await panel.getByRole('button', { name: 'Withdraw' }).click();
	await expect(panel.getByText('canonical', { exact: true })).not.toBeVisible();
	await expect(panel.getByRole('button', { name: 'Pick', exact: true })).toHaveCount(2);
});

test('adjudication: non-accepted replicas offer no Pick at all', async ({ page }) => {
	await snapshot(
		page,
		{ project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling },
		listing([
			task('g1-r1', 'claimed', { replica_of: 'g1', assignee: 'dave' }),
			task('g1-r2', 'in_review', { replica_of: 'g1', submitted_by: 'gina' }),
		]),
	);

	await page.goto('/annotator/projects/p1');

	const panel = page.getByTestId('adjudication-panel');
	await expect(panel.getByText('g1')).toBeVisible();
	await expect(panel.getByRole('button', { name: /Pick|Re-pick/ })).toHaveCount(0);
});

test('adjudication: a stale-pick 409 from the server surfaces verbatim', async ({ page }) => {
	await snapshot(
		page,
		{ project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling },
		listing([task('g1-r1', 'accepted', { replica_of: 'g1' })]),
	);
	await seed(page, {
		'PUT /projects/p1/adjudications/g1': {
			status: 409,
			body: {
				detail: 'adjudicate (g1-r1 is skipped, not accepted — only accepted work can be canonical)',
			},
		},
	});

	await page.goto('/annotator/projects/p1');
	await page
		.getByTestId('adjudication-panel')
		.getByRole('button', { name: 'Pick', exact: true })
		.click();

	await expect(page.getByText(/only accepted work can be canonical/)).toBeVisible();
});

test('instructions: the create dialog sends them and the detail page shows them to annotators', async ({
	page,
}) => {
	await seed(page, {
		'GET /projects': { projects: [], total: 0 },
		'POST /projects': project('draft'),
	});
	await snapshot(
		page,
		{
			project: project('labeling', { instructions: 'Label every visible portrait; skip seals.' }),
			legal_events: LEGAL.labeling,
		},
		listing([]),
	);

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();
	const dialog = page.getByRole('dialog');
	await dialog.getByPlaceholder('vasa-portraits').fill('vasa-portraits');
	await dialog.getByPlaceholder(/skip seals and marginalia/).fill('Portraits only; ignore seals.');
	// See the consensus test: the binding must have landed before Enter submits the form.
	await expect(dialog.getByPlaceholder(/skip seals and marginalia/)).toHaveValue(
		'Portraits only; ignore seals.',
	);
	await dialog.getByPlaceholder('vasa-portraits').press('Enter');

	const create = await createCall(page);
	expect((create.body as { instructions: string }).instructions).toBe(
		'Portraits only; ignore seals.',
	);

	// And the detail page renders them where the work is picked up.
	await page.goto('/annotator/projects/p1');
	await expect(page.getByTestId('instructions')).toContainText(
		'Label every visible portrait; skip seals.',
	);
});

// --------------------------------------------------------------------------------------------------
// The task ONTOLOGY — a preset picked at create travels as ONE document, class list included
// --------------------------------------------------------------------------------------------------

test('ontology: picking a task type sends ONE document; the detail page wears it', async ({
	page,
}) => {
	await seed(page, {
		'GET /projects': { projects: [], total: 0 },
		'POST /projects': project('draft'),
	});
	await snapshot(
		page,
		{
			project: project('labeling', {
				ontology: {
					kind: 'reading-order',
					modality: 'image',
					classes: [
						{
							name: 'region',
							tools: ['bbox'],
							attributes: [{ name: 'order', type: 'int', required: true }],
							required: true,
						},
					],
					relations: [],
					allow_empty: false,
				},
			}),
			legal_events: LEGAL.labeling,
		},
		listing([]),
	);

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();
	const dialog = page.getByRole('dialog');
	await dialog.getByPlaceholder('vasa-portraits').fill('reading-order-live');
	await dialog.getByPlaceholder('person, ship, signature').fill('region');
	// See the consensus test: the binding must have landed before Enter submits the form.
	await expect(dialog.getByPlaceholder('person, ship, signature')).toHaveValue('region');
	// KEYBOARD selection, deliberately: Bits UI's select portal keeps `pointer-events: none` on
	// <body> after a mouse pick (verified in a live browser), so a subsequent click in the dialog is
	// intercepted forever. Typing the option name and pressing Enter both dodges that AND exercises
	// the path a keyboard user takes.
	await dialog.getByLabel('Task type').press('Enter');
	await page.getByRole('option', { name: 'reading-order' }).waitFor();
	await dialog.getByLabel('Task type').press('r');
	await dialog.getByLabel('Task type').press('Enter');
	await expect(dialog.getByLabel('Task type')).toContainText('reading-order');
	// Submit by Enter in a text field (implicit form submission), not by clicking: Bits UI's select
	// leaves `pointer-events: none` on <body> after a pick, so any later CLICK in this dialog is
	// intercepted forever (verified in a live browser). Keyboard is unaffected — and is the path a
	// keyboard user takes anyway.
	await dialog.getByPlaceholder('vasa-portraits').press('Enter');

	const create = await createCall(page);
	const ontology = (create.body as { ontology: Record<string, unknown> }).ontology;
	// ONE payload. This used to assert a `template` while a SECOND `label_schema` built its own
	// class list from the same textarea — two objects nothing cross-checked, which is the defect
	// this model closes. The tools now live ON the class, so they cannot name a shape the taxonomy
	// does not permit.
	expect(ontology).toMatchObject({ kind: 'reading-order' });
	const classes = ontology.classes as Record<string, unknown>[];
	expect(classes).toHaveLength(1);
	expect(classes[0]).toMatchObject({ name: 'region', tools: ['bbox'] });
	expect((classes[0]!.attributes as unknown[])[0]).toMatchObject({
		name: 'order',
		type: 'int',
		required: true,
	});
	// NOT required — the create dialog used to derive `required_labels` from every class name, so a
	// third class silently made all three mandatory on every item. It is now an explicit toggle,
	// and this test does not tick it.
	expect(classes[0]).toMatchObject({ required: false });

	// The detail page names the task type and the taxonomy, from the same document.
	await page.goto('/annotator/projects/p1');
	await expect(page.getByTestId('task-kind-chip')).toContainText('reading-order');
	await expect(page.getByTestId('label-taxonomy')).toContainText('region');
	await expect(page.getByTestId('label-taxonomy')).toContainText('bbox');
});

test('ontology: "every class is required" is an explicit choice, not a side effect of naming classes', async ({
	page,
}) => {
	// The bug this replaced, pinned from the UI side: the dialog derived a project-level
	// `required_labels` from EVERY class name, so adding a third class silently made all three
	// mandatory on every item — a contract nobody wrote, discovered at submit.
	await seed(page, {
		'GET /projects': { projects: [], total: 0 },
		'POST /projects': project('draft'),
	});

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();
	const dialog = page.getByRole('dialog');
	await dialog.getByPlaceholder('vasa-portraits').fill('required-classes');
	await dialog.getByPlaceholder('person, ship, signature').fill('person, ship, signature');
	await expect(dialog.getByPlaceholder('person, ship, signature')).toHaveValue(
		'person, ship, signature',
	);
	await dialog.getByLabel('Every class is required').check();
	await dialog.getByPlaceholder('vasa-portraits').press('Enter');

	const create = await createCall(page);
	const classes = (create.body as { ontology: { classes: Record<string, unknown>[] } }).ontology
		.classes;
	expect(classes.map((c) => c.name)).toEqual(['person', 'ship', 'signature']);
	expect(classes.every((c) => c.required === true)).toBe(true);
});

test('ontology: a manager edits the taxonomy after create, and the PATCH carries the whole document', async ({
	page,
}) => {
	// There was no way to edit an ontology at all — it was set at create and never again, so a
	// taxonomy typo meant a new project. A closed set you cannot correct is one people work around.
	await snapshot(
		page,
		{
			project: project('labeling', {
				ontology: {
					kind: 'object-detection',
					modality: 'image',
					classes: [{ name: 'shp', tools: ['bbox'], attributes: [], required: false }],
					relations: [],
					allow_empty: false,
				},
			}),
			legal_events: LEGAL.labeling,
		},
		listing([]),
	);
	await seed(page, { 'PATCH /projects/p1/ontology': project('labeling') });

	await page.goto('/annotator/projects/p1');
	await page.getByTestId('edit-ontology-trigger').click();

	// The form opens SEEDED from the stored ontology — an edit surface that starts blank is a
	// replace surface wearing an edit label, and this is a whole-document PUT underneath.
	const classes = page.getByTestId('edit-ontology').getByPlaceholder('person, ship, signature');
	await expect(classes).toHaveValue('shp');

	await classes.fill('ship, person');
	await expect(classes).toHaveValue('ship, person');
	await page.getByTestId('edit-ontology').getByRole('button', { name: 'Save' }).click();

	await expect
		.poll(async () => (await calls(page)).filter((c) => c.method === 'PATCH').length, {
			timeout: 10_000,
		})
		.toBe(1);
	const patch = (await calls(page)).find((c) => c.method === 'PATCH')!;

	expect(patch.path).toBe('/projects/p1/ontology');
	const sent = (patch.body as { ontology: { classes: { name: string }[] } }).ontology;
	expect(sent.classes.map((c) => c.name)).toEqual(['ship', 'person']);
});

test('ontology: the editor REFUSES to flatten structure it cannot express', async ({ page }) => {
	// The form offers three fields. An ontology carrying relations or per-class attributes has
	// structure this form would silently drop on save — so it declines rather than losing it. A
	// half-editor that quietly flattens a document is worse than no editor.
	await snapshot(
		page,
		{
			project: project('labeling', {
				ontology: {
					kind: 'document-question-answering',
					modality: 'image',
					classes: [
						{ name: 'key', tools: ['bbox'], attributes: [], required: false },
						{ name: 'value', tools: ['bbox'], attributes: [], required: false },
					],
					relations: [
						{
							name: 'answers',
							from_classes: ['key'],
							to_classes: ['value'],
							directed: true,
							required: false,
						},
					],
					allow_empty: false,
				},
			}),
			legal_events: LEGAL.labeling,
		},
		listing([]),
	);

	await page.goto('/annotator/projects/p1');
	await page.getByTestId('edit-ontology-trigger').click();

	await expect(page.getByTestId('ontology-too-rich')).toContainText('relation');
	await expect(page.getByTestId('edit-ontology').getByRole('button', { name: 'Save' })).toHaveCount(
		0,
	);
});

test('ontology: the edit button is ABSENT once the project is frozen', async ({ page }) => {
	// Mirrors the server's own gate. Past `frozen` a publish is being prepared against the answer
	// set, so an edit could only be ignored (every remaining item captured its copy) or misleading —
	// the run facet would report a taxonomy no task was ever judged against. The route still 409s;
	// this only stops offering the door.
	await snapshot(page, { project: project('frozen'), legal_events: LEGAL.labeling }, listing([]));

	await page.goto('/annotator/projects/p1');
	await expect(page.getByTestId('edit-ontology-trigger')).toHaveCount(0);
});

test('an unfinishable item can be REMOVED — otherwise one of them wedges the publish forever', async ({
	page,
}) => {
	// The publish precondition requires EVERY task terminal. An item naming a media dataset that was
	// renamed or removed cannot be opened, so it can never be claimed, submitted or skipped — and
	// before this the only way past it was to abandon the project and re-send everything.
	await snapshot(
		page,
		{ project: project('labeling'), legal_events: LEGAL.labeling },
		listing([task('t1', 'unassigned')]),
	);
	await seed(page, {
		'DELETE /projects/p1/tasks/t1': { task_id: 't1', removed: true, total: 0 },
	});
	page.on('dialog', (d) => void d.accept());

	await page.goto('/annotator/projects/p1');
	await page.getByTestId('drop-task').first().click();

	await expect
		.poll(async () => (await calls(page)).filter((c) => c.method === 'DELETE').length, {
			timeout: 10_000,
		})
		.toBe(1);
	const del = (await calls(page)).find((c) => c.method === 'DELETE')!;
	expect(del.path).toBe('/projects/p1/tasks/t1');
});

test('the remove control is ABSENT once the project is frozen', async ({ page }) => {
	// Mirrors the server's own `DROPPABLE_STATES`. Past `frozen` a publish is being prepared against
	// the answer set, so removing an item would change what the run facet describes after the
	// description was fixed. The route still 409s; this only stops offering the door.
	await snapshot(
		page,
		{ project: project('frozen'), legal_events: LEGAL.labeling },
		listing([task('t1', 'accepted')]),
	);

	await page.goto('/annotator/projects/p1');
	await expect(page.getByTestId('drop-task')).toHaveCount(0);
});
