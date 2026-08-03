import { test, expect, type Page, type Route } from '@playwright/test';

// Hermetic coverage for the annotation task-management surfaces (OPEN-WORK.md § Design — annotation projects; the A1–A4 surfaces).
// Every backend response is mocked at the zone-scoped BFF boundary; the backend's OWN
// contracts (FGA doors, machine tables, saga idempotency) are pinned by tests/unit/*.
// What THIS layer proves: the UI renders the transitions the backend supplies, drives
// the right endpoints with the right bodies, keeps the three review actions distinct,
// states what a publish lands before firing it, narrates a running publish, and
// surfaces a server 403 as the refusal it is.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const KEY = 'fe00cd746463ad2c/0/19';

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
	state: keyof typeof LEGAL | 'publishing' | 'publish_failed',
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

let writes: { path: string; body: unknown }[] = [];

async function baseMocks(page: Page): Promise<void> {
	writes = [];
	await page.route('**/annotator/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.route('**/annotator/api/**', (route) => {
		const req = route.request();
		if (req.method() !== 'GET') {
			writes.push({ path: new URL(req.url()).pathname, body: req.postDataJSON() });
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
}

// --------------------------------------------------------------------------------------------------
// A1 · the landing
// --------------------------------------------------------------------------------------------------

test('A1: the landing lists the tenant’s projects with state and progress', async ({ page }) => {
	await baseMocks(page);
	await page.route('**/annotator/api/projects?tenant=*', (route) =>
		json(route, { projects: [project('labeling')], total: 1 }),
	);

	await page.goto('/annotator/');

	await expect(page.getByRole('heading', { name: 'Labeling tasks' })).toBeVisible();
	const card = page.getByRole('link', { name: /Vasa portraits/ });
	await expect(card).toBeVisible();
	await expect(card.getByText('labeling')).toBeVisible();
	await expect(card.getByText('1/3 items terminal')).toBeVisible();
});

test('A1: a refused list is a REFUSAL, not an empty state', async ({ page }) => {
	await baseMocks(page);
	await page.route('**/annotator/api/projects?tenant=*', (route) =>
		json(route, { detail: 'gina lacks member on project:default' }, 403),
	);

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
	await baseMocks(page);
	// Signed in as `anon` (LIFO: this registration wins over baseMocks' 401) — the lease chip's
	// "yours" reads ME against the assignee, and a signed-out UI honestly can't say "yours".
	await page.route('**/annotator/capi/v1/me', (route) =>
		json(route, { sub: 'anon', name: null, email: null, estate_admin: true, projects: [] }),
	);
	// Stateful double: the claim/submit POSTs move t1 through the machine, and the page's
	// refetch reads the moved state — the UI never invents a transition itself.
	let t1 = task('t1', 'unassigned');
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([t1])),
	);
	await page.route('**/annotator/api/tasks/t1/events', (route) => {
		const body = route.request().postDataJSON() as { event: string };
		writes.push({ path: '/annotator/api/tasks/t1/events', body });
		if (body.event === 'claim') {
			t1 = task('t1', 'claimed', {
				assignee: 'anon',
				lease_expires_at: new Date(Date.now() + 600_000).toISOString(),
			});
		} else if (body.event === 'submit') {
			t1 = task('t1', 'in_review', { submitted_by: 'anon' });
		}
		return json(route, t1);
	});

	await page.goto('/annotator/projects/p1');
	await expect(page.getByRole('heading', { name: /Vasa portraits/ })).toBeVisible();

	// Claim: the button comes from the task's OWN legal_events.
	await page.getByRole('button', { name: 'Claim' }).click();
	expect(writes.map((w) => w.body)).toContainEqual({ event: 'claim' });

	// The refetched row is claimed with a live lease — and Annotate routes into the canvas
	// with the task's OWN keys (`?keys=`), not a second viewer. (By title: the estate navbar
	// also carries an "Annotate" zone link.)
	await expect(page.getByText(/yours · \d{2}:\d{2}/)).toBeVisible();
	const annotate = page.getByTitle('open this item on the annotate canvas');
	await expect(annotate).toHaveAttribute('href', new RegExp(`keys=${encodeURIComponent(KEY)}`));

	// Submit for review — the working loop's handoff.
	await page.getByRole('button', { name: 'Submit for review' }).click();
	expect(writes.map((w) => w.body)).toContainEqual({ event: 'submit' });
	await expect(page.getByText('in review')).toBeVisible();
});

test('A2: an expired lease is shown EXPIRED, never as held', async ({ page }) => {
	await baseMocks(page);
	const stale = task('t1', 'claimed', {
		assignee: 'dave',
		lease_expires_at: new Date(Date.now() - 60_000).toISOString(),
	});
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([stale])),
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
	await baseMocks(page);
	const inReview = task('t2', 'in_review', { submitted_by: 'gina' });
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([inReview])),
	);
	await page.route('**/annotator/api/tasks/t2/events', (route) => {
		writes.push({ path: '/annotator/api/tasks/t2/events', body: route.request().postDataJSON() });
		return json(route, task('t2', 'changes_requested', { submitted_by: 'gina' }));
	});

	await page.goto('/annotator/projects/p1');

	// All three, simultaneously visible, separately actionable — never collapsed.
	await expect(page.getByRole('button', { name: 'Accept', exact: true })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Fix & accept', exact: true })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Request changes…' })).toBeVisible();

	await page.getByRole('button', { name: 'Request changes…' }).click();
	await page.getByPlaceholder(/stamp in the corner/).fill('The stamp in the corner is unlabelled');
	await page.getByRole('button', { name: 'Request changes', exact: true }).click();

	expect(writes.map((w) => w.body)).toContainEqual({
		event: 'request_changes',
		message: 'The stamp in the corner is unlabelled',
	});
	// The distinct-edges guarantee: nothing here fired accept or fix_and_accept.
	expect(writes.map((w) => (w.body as { event: string }).event)).not.toContain('accept');
	expect(writes.map((w) => (w.body as { event: string }).event)).not.toContain('fix_and_accept');
});

// --------------------------------------------------------------------------------------------------
// A4 · publish — confirm states the contract; progress is narrated; failure offers retry
// --------------------------------------------------------------------------------------------------

test('A4: the confirm step states what lands and whose names travel; a running publish narrates; failure offers retry', async ({
	page,
}) => {
	await baseMocks(page);
	const done = [
		task('t1', 'accepted', {
			submitted_by: 'gina',
			reviewed_by: 'carol',
			review_action: 'accepted',
		}),
		task('t2', 'skipped'),
	];
	// Phases: frozen → (publish POST) → publishing (narrating) → publish_failed (with reason).
	let phase: 'frozen' | 'publishing' | 'failed' = 'frozen';
	let publishingReads = 0;
	await page.route('**/annotator/api/projects/p1', (route) => {
		if (phase === 'frozen') {
			return json(route, { project: project('frozen'), legal_events: LEGAL.frozen });
		}
		if (phase === 'publishing') {
			publishingReads += 1;
			if (publishingReads >= 2) phase = 'failed';
			return json(route, {
				project: project('publishing', {
					publish_progress: 'creating table silver$vasa-portraits_0123456789ab',
				}),
				legal_events: [],
			});
		}
		return json(route, {
			project: project('publish_failed', {
				publish_error: 'catalog unreachable: connection refused',
				publish_progress: 'creating table silver$vasa-portraits_0123456789ab',
				pending_target_namespace: 'silver',
			}),
			legal_events: [{ event: 'publish', to: 'publishing', permission: 'can_publish' }],
		});
	});
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing(done)),
	);
	await page.route('**/annotator/api/projects/p1/events', (route) => {
		writes.push({
			path: '/annotator/api/projects/p1/events',
			body: route.request().postDataJSON(),
		});
		phase = 'publishing';
		return json(route, project('publishing'));
	});

	await page.goto('/annotator/projects/p1');

	await page.getByRole('button', { name: 'Publish…' }).click();
	// The confirm step BEFORE anything runs: counts, sentinel honesty, and the names.
	// (Scoped to the dialog; template line breaks mean a `.*` regex can't span the phrases.)
	const dialog = page.getByRole('dialog');
	await expect(dialog.getByText(/accepted item/)).toBeVisible();
	await expect(dialog.getByText(/sentinel rows/)).toBeVisible();
	await expect(dialog.getByText(/gina/)).toBeVisible();
	await expect(dialog.getByText(/carol/)).toBeVisible();

	await page.getByRole('button', { name: 'Publish to silver' }).click();
	expect(writes.map((w) => w.body)).toContainEqual({
		event: 'publish',
		target_namespace: 'silver',
	});

	// The RUNNING publish narrates the saga's actual step — not a spinner.
	await expect(page.getByText('creating table silver$vasa-portraits_0123456789ab')).toBeVisible();

	// The failure shows the recorded error, the step it died at, and a retry that restates
	// the pinned target namespace. (The page's 2s poll carries the phase transitions.)
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
	await baseMocks(page);
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([task('t1', 'unassigned')])),
	);
	await page.route('**/annotator/api/tasks/t1/events', (route) =>
		json(route, { detail: 'gina lacks can_claim on annotation_project:p1' }, 403),
	);

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
	await baseMocks(page);
	const reviewables = [
		task('t1', 'in_review', { submitted_by: 'gina' }),
		task('t2', 'in_review', { submitted_by: 'gina' }),
		task('t3', 'unassigned'),
	];
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing(reviewables)),
	);
	for (const id of ['t1', 't2']) {
		await page.route(`**/annotator/api/tasks/${id}/events`, (route) => {
			writes.push({
				path: `/annotator/api/tasks/${id}/events`,
				body: route.request().postDataJSON(),
			});
			return json(route, task(id, 'accepted', { submitted_by: 'gina', reviewed_by: 'carol' }));
		});
	}

	await page.goto('/annotator/projects/p1');
	await page.getByRole('checkbox', { name: 'Select all' }).check();
	await expect(page.getByTestId('bulk-bar')).toContainText('3 selected · 2 reviewable');
	await page.getByRole('button', { name: 'Accept 2 reviewed' }).click();
	await expect(page.getByText('Accepted 2 items.')).toBeVisible();

	// Exactly the two in_review tasks were accepted — the unassigned one was never fired at.
	const accepted = writes.filter((w) => (w.body as { event: string }).event === 'accept');
	expect(accepted.map((w) => w.path).sort()).toEqual([
		'/annotator/api/tasks/t1/events',
		'/annotator/api/tasks/t2/events',
	]);
});

// --------------------------------------------------------------------------------------------------
// Assignment — the manager's distribution edge
// --------------------------------------------------------------------------------------------------

test('assign names a recipient and the row comes back pinned', async ({ page }) => {
	await baseMocks(page);
	let t1 = task('t1', 'unassigned', {});
	t1 = {
		...t1,
		legal_events: [
			{ event: 'claim', to: 'claimed', permission: 'can_claim' },
			{ event: 'assign', to: 'claimed', permission: 'can_manage' },
		],
	};
	let current = t1;
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling'), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([current])),
	);
	await page.route('**/annotator/api/tasks/t1/events', (route) => {
		const body = route.request().postDataJSON() as { event: string; assignee?: string };
		writes.push({ path: '/annotator/api/tasks/t1/events', body });
		// The server pins an assigned item: claimed, named assignee, NO lease expiry (§5.2).
		current = task('t1', 'claimed', { assignee: body.assignee, lease_expires_at: null });
		return json(route, current);
	});

	await page.goto('/annotator/projects/p1');
	await page.getByRole('button', { name: 'Assign…' }).click();
	await page.getByPlaceholder(/annotator \(OIDC subject/).fill('dave');
	await page.getByRole('button', { name: 'Assign', exact: true }).click();

	expect(writes.map((w) => w.body)).toContainEqual({ event: 'assign', assignee: 'dave' });
	// The pinned chip: held by dave, no countdown — an assignment never expires.
	await expect(page.getByText('dave · pinned')).toBeVisible();
});

// --------------------------------------------------------------------------------------------------
// Consensus v1 — replica items (B)
// --------------------------------------------------------------------------------------------------

test('consensus: the create dialog carries the field and the create POST carries consensus_n', async ({
	page,
}) => {
	await baseMocks(page);
	await page.route('**/annotator/api/projects?tenant=*', (route) =>
		json(route, { projects: [], total: 0 }),
	);
	// The create POST carries no query string, so it needs its own route registration.
	await page.route('**/annotator/api/projects', (route) => {
		writes.push({ path: '/annotator/api/projects', body: route.request().postDataJSON() });
		return json(route, project('draft', { consensus_n: 3 }));
	});

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();

	const dialog = page.getByRole('dialog');
	await expect(dialog.getByText(/annotators per item/)).toBeVisible();
	await dialog.getByPlaceholder('vasa-portraits').fill('vasa-portraits');
	await dialog.getByRole('spinbutton').fill('3');
	await dialog.getByRole('button', { name: 'Create labeling task' }).click();

	const create = writes.find((w) => w.path === '/annotator/api/projects');
	expect(create).toBeDefined();
	expect((create!.body as { consensus_n: number }).consensus_n).toBe(3);
});

test('consensus: replica items wear a replica k/N chip from their deterministic ids', async ({
	page,
}) => {
	await baseMocks(page);
	const replicas = [
		task('g1-r1', 'unassigned', { replica_of: 'g1' }),
		task('g1-r2', 'claimed', { replica_of: 'g1', assignee: 'dave' }),
		task('t9', 'unassigned'), // an ordinary item — no chip
	];
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, {
			project: project('labeling', { consensus_n: 2 }),
			legal_events: LEGAL.labeling,
		}),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing(replicas)),
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
	await baseMocks(page);
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, {
			project: project('labeling', { consensus_n: 2 }),
			legal_events: LEGAL.labeling,
		}),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([task('g1-r2', 'unassigned', { replica_of: 'g1' })])),
	);
	await page.route('**/annotator/api/tasks/g1-r2/events', (route) =>
		json(
			route,
			{
				detail:
					'one replica per annotator per group: gina already holds or worked replica g1-r1 of group g1',
			},
			409,
		),
	);

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
	await baseMocks(page);
	// Stateful double: the PUT records the pick, and the refetched project carries it — the UI
	// never marks a replica canonical on its own.
	let adjudications: Record<string, { task_id: string; by: string; at: string }> = {};
	const replicas = [
		task('g1-r1', 'accepted', { replica_of: 'g1', submitted_by: 'gina' }),
		task('g1-r2', 'accepted', { replica_of: 'g1', submitted_by: 'dave' }),
	];
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, {
			project: project('labeling', { consensus_n: 2, adjudications }),
			legal_events: LEGAL.labeling,
		}),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing(replicas)),
	);
	await page.route('**/annotator/api/projects/p1/adjudications/g1', (route) => {
		if (route.request().method() === 'DELETE') {
			adjudications = {};
			return json(route, project('labeling', { consensus_n: 2, adjudications }));
		}
		const body = route.request().postDataJSON() as { task_id: string };
		writes.push({ path: '/annotator/api/projects/p1/adjudications/g1', body });
		adjudications = { g1: { task_id: body.task_id, by: 'anon', at: new Date().toISOString() } };
		return json(route, project('labeling', { consensus_n: 2, adjudications }));
	});

	await page.goto('/annotator/projects/p1');

	const panel = page.getByTestId('adjudication-panel');
	await expect(panel.getByText('Adjudication')).toBeVisible();
	await expect(panel.getByText('gina')).toBeVisible();
	await expect(panel.getByText('dave')).toBeVisible();

	await panel.getByRole('button', { name: 'Pick', exact: true }).first().click();

	expect(writes.map((w) => w.body)).toContainEqual({ task_id: 'g1-r1' });
	// The refetched pick marks the replica in BOTH surfaces: the panel and the queue row.
	await expect(panel.getByText('canonical', { exact: true })).toBeVisible();
	await expect(page.getByRole('table').getByText('canonical')).toBeVisible();
	// The runner-up stays re-pickable — a pick is pre-publish metadata, not a ratchet.
	await expect(panel.getByRole('button', { name: 'Re-pick' })).toBeVisible();

	// Withdraw (the un-wedge path): DELETE clears the pick and the chips go with it.
	await panel.getByRole('button', { name: 'Withdraw' }).click();
	await expect(panel.getByText('canonical', { exact: true })).not.toBeVisible();
	await expect(panel.getByRole('button', { name: 'Pick', exact: true })).toHaveCount(2);
});

test('adjudication: non-accepted replicas offer no Pick at all', async ({ page }) => {
	await baseMocks(page);
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(
			route,
			listing([
				task('g1-r1', 'claimed', { replica_of: 'g1', assignee: 'dave' }),
				task('g1-r2', 'in_review', { replica_of: 'g1', submitted_by: 'gina' }),
			]),
		),
	);

	await page.goto('/annotator/projects/p1');

	const panel = page.getByTestId('adjudication-panel');
	await expect(panel.getByText('g1')).toBeVisible();
	await expect(panel.getByRole('button', { name: /Pick|Re-pick/ })).toHaveCount(0);
});

test('adjudication: a stale-pick 409 from the server surfaces verbatim', async ({ page }) => {
	await baseMocks(page);
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, { project: project('labeling', { consensus_n: 2 }), legal_events: LEGAL.labeling }),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([task('g1-r1', 'accepted', { replica_of: 'g1' })])),
	);
	await page.route('**/annotator/api/projects/p1/adjudications/g1', (route) =>
		json(
			route,
			{
				detail: 'adjudicate (g1-r1 is skipped, not accepted — only accepted work can be canonical)',
			},
			409,
		),
	);

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
	await baseMocks(page);
	await page.route('**/annotator/api/projects?tenant=*', (route) =>
		json(route, { projects: [], total: 0 }),
	);
	await page.route('**/annotator/api/projects', (route) => {
		writes.push({ path: '/annotator/api/projects', body: route.request().postDataJSON() });
		return json(route, project('draft'));
	});
	await page.route('**/annotator/api/projects/p1', (route) =>
		json(route, {
			project: project('labeling', { instructions: 'Label every visible portrait; skip seals.' }),
			legal_events: LEGAL.labeling,
		}),
	);
	await page.route('**/annotator/api/projects/p1/tasks?include=details', (route) =>
		json(route, listing([])),
	);

	await page.goto('/annotator/');
	await page.getByRole('button', { name: 'New labeling task' }).first().click();
	const dialog = page.getByRole('dialog');
	await dialog.getByPlaceholder('vasa-portraits').fill('vasa-portraits');
	await dialog.getByPlaceholder(/skip seals and marginalia/).fill('Portraits only; ignore seals.');
	await dialog.getByRole('button', { name: 'Create labeling task' }).click();

	const create = writes.find((w) => w.path === '/annotator/api/projects');
	expect(create).toBeDefined();
	expect((create!.body as { instructions: string }).instructions).toBe(
		'Portraits only; ignore seals.',
	);

	// And the detail page renders them where the work is picked up.
	await page.goto('/annotator/projects/p1');
	await expect(page.getByTestId('instructions')).toContainText(
		'Label every visible portrait; skip seals.',
	);
});
