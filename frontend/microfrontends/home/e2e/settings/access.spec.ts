import { expect, test } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';
import { MOCK_CATALOG } from '../ports';

// `/settings/access` — the ONE query-driven FGA explorer, in the HOME zone since #105.
//
// It is PLATFORM configuration, not a lakehouse feature: it reads the whole tuple store across every
// project and takes none. (The lakehouse keeps its PER-OBJECT grants plane — the access tab on one
// table or namespace — which is a different question at a different level and has its own specs.)
//
// This replaced four tabs (Graph / Tuples / Check / Model) that shared no state and no URL, and the
// spec replaced with it: what is exercised now is the WORKFLOW those tabs made impossible — ask a
// question, see the answer lit on a canvas that never unmounts, narrow it without refetching, and read
// the selected node beside it rather than instead of it.
//
// Hermetic via the MOCK CATALOG, not page.route: the surface reads through remote functions
// (`remote/access.remote.ts`), so every request happens on the zone SERVER, where page.route cannot
// reach. The fixtures live in `access-fixtures.ts`
// (shared with `../mock-catalog.ts`), and the REQUEST shapes — still the frozen /v1/access contract — are
// recorded by the mock per bearer and asserted through `/__mock/access`. Each test signs in with a
// per-test bearer so its ledger on the fullyParallel suite's one shared mock is its own.

type Body = Record<string, unknown>;
type AccessState = {
	extraTuples: Body[];
	written: Body[];
	deleted: Body[];
	expandBody: Body | null;
	listUsersBodies: Body[];
	listObjectsBody: Body | null;
	simulateBodies: Body[];
	/** How many times this bearer's control cursor has polled `/v1/events` — the live spec's settle gate. */
	eventProbes: number;
};

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	// The estate-admin identity the `/settings` layout door reads. SEEDED on the mock catalog rather
	// than page.route'd: this zone's layout calls `fetchMe` against CATALOG_API directly, so there is no
	// browser hop to intercept.
	await seedFor(page, token, { 'GET /v1/me': ME_ADMIN });
});

/** What the mock catalog recorded for THIS test's bearer — the replacement for the capture variables
 *  page.route used to fill while the transport was still browser-side. */
const accessState = async (page: import('@playwright/test').Page): Promise<AccessState> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/access`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return (await res.json()) as AccessState;
};

const open = async (page: import('@playwright/test').Page) => {
	await page.goto('/settings/access');
	// 20s, not the 5s `expect` default: this route pulls in Svelte Flow and its first paint on a dev
	// server costs a full cold compile, which the per-test budget (30s) allows for and the per-assertion
	// default does not.
	//
	// It is NOT the whole story, and the extra timeout does not make this deterministic — measured. When
	// this line does fail, the page snapshot shows the shell rendered and `<main>` EMPTY, with the
	// browser reporting `Failed to fetch dynamically imported module:
	// .svelte-kit/generated/client/nodes/*.js`. That is the page node being rewritten underneath the
	// browser mid-fetch, i.e. something else rebuilding this tree concurrently (measured: 137 writes
	// into .svelte-kit in two minutes, 10 vite processes). No timeout fixes a module that 404s.
	// The suite runs green on a quiet tree; a concurrent `turbo run check`/build is what breaks it.
	await expect(page.getByRole('heading', { name: 'Access', level: 1 })).toBeVisible({
		timeout: 20_000,
	});
};

/**
 * Flip the Data/Model toggle, retried against the PRE-HYDRATION race.
 *
 * The heading `open()` waits for is server-rendered, so it can be visible while the client has not yet
 * attached its listeners — and a click on an un-hydrated `<button>` does nothing at all, silently. Every
 * other interaction in this file is followed by an auto-retrying `expect`, which absorbs the race; the
 * view toggle is the one whose ONLY evidence is the click itself, so it retries here instead. The idiom
 * is the estate's (`lakehouse/e2e/lineage/shell.spec.ts`'s `openPanel`, same race, same shape).
 */
const pressView = async (page: import('@playwright/test').Page, label: string) => {
	const button = page.getByRole('button', { name: label });
	await expect(async () => {
		await button.click();
		await expect(button).toHaveAttribute('aria-pressed', 'true', { timeout: 1000 });
	}).toPass({ timeout: 20_000 });
};

const ask = async (
	page: import('@playwright/test').Page,
	shape: 'What can…' | 'Who can…' | 'Why…',
	fields: Partial<Record<'Subject' | 'Relation' | 'Object' | 'Object type', string>>,
) => {
	await page.getByRole('button', { name: shape }).click();
	for (const [label, value] of Object.entries(fields)) {
		await page.getByLabel(label, { exact: true }).fill(value);
	}
	await page.getByRole('button', { name: 'Run' }).click();
};

test('the four tabs are gone — one canvas, one query bar, one inspector', async ({ page }) => {
	await open(page);
	// The old view's tab strip. Its absence IS the requirement: these were four destinations that could
	// not be on screen together, which is what made the "why" workflow a retyping exercise.
	await expect(page.getByRole('tablist')).toHaveCount(0);
	for (const name of ['Graph', 'Tuples', 'Check', 'Model']) {
		await expect(page.getByRole('tab', { name })).toHaveCount(0);
	}
	await expect(page.getByRole('button', { name: 'What can…' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Who can…' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Why…' })).toBeVisible();
	await expect(page.getByText('Filter', { exact: true })).toBeVisible();
});

test('"why" runs Check + Expand and lights the derivation hop by hop', async ({ page }) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});

	// The verdict is the fact…
	await expect(page.getByText('ALLOWED')).toBeVisible();
	// …and the derivation is the explanation. A one-level Expand could not produce this: it comes from
	// following `owner from parent` into namespace:db1, which is the whole point of `depth`.
	await expect(page.getByRole('heading', { name: 'Derivation' })).toBeVisible();
	await expect(page.getByText('inherited from').first()).toBeVisible();

	// The request shape is the contract.
	expect((await accessState(page)).expandBody).toMatchObject({
		object: 'table:db1$t',
		relation: 'can_read_data',
		depth: 3,
	});
});

test('"who can" answers from ListUsers, usersets included', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });

	await expect(page.getByText('3 subject(s) hold reader')).toBeVisible();
	// The userset arm — `role:validators#assignee` — is the one the old wrapper discarded outright.
	await expect(
		page.locator('[data-slot="access-node"]').filter({ hasText: 'validators' }),
	).toBeVisible();
	expect((await accessState(page)).listUsersBodies[0]).toMatchObject({
		object: 'table:db1$t',
		relation: 'reader',
	});
});

test('"what can" answers from ListObjects', async ({ page }) => {
	await open(page);
	await ask(page, 'What can…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		'Object type': 'table',
	});

	await expect(page.getByText('holds can_read_data on 2 table object(s)')).toBeVisible();
	expect((await accessState(page)).listObjectsBody).toMatchObject({
		user: 'user:alice',
		relation: 'can_read_data',
		type: 'table',
	});
});

test('the facet rail filters what is already rendered — no refetch', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	await expect(page.getByText('3 subject(s) hold reader')).toBeVisible();

	const before = (await accessState(page)).listUsersBodies.length;
	const nodesBefore = await page.locator('[data-slot="access-node"]').count();

	// Turning a type facet on narrows the canvas… (the facets live in a popover now — the resident
	// 208px rail spent the page's scarcest resource, canvas width, on an idle control)
	await page.getByRole('button', { name: 'Filter' }).click();
	await page.getByLabel('Filter by type role').click();
	await expect(page.getByRole('button', { name: 'Clear filters' })).toBeVisible();
	await expect
		.poll(async () => page.locator('[data-slot="access-node"]').count())
		.toBeLessThan(nodesBefore);

	// …and costs ZERO further requests. Filtering that refetches is a different feature, and a much
	// slower one, than filtering what you are already looking at.
	expect((await accessState(page)).listUsersBodies.length).toBe(before);
});

test('query, seed and facets live in the URL, so the view is a link', async ({ page }) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();

	const url = new URL(page.url());
	expect(url.searchParams.get('q')).toBe('why');
	expect(url.searchParams.get('user')).toBe('user:alice');
	expect(url.searchParams.get('relation')).toBe('can_read_data');
	expect(url.searchParams.get('object')).toBe('table:db1$t');
	// No synthetic bookkeeping parameter: the link is the query and nothing else.
	expect(url.searchParams.get('_w')).toBeNull();

	// And it round-trips — a cold load of that URL reproduces the same answer.
	await page.goto(url.toString());
	await expect(page.getByText('ALLOWED')).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Derivation' })).toBeVisible();
});

test('revoke states the blast radius BEFORE the write', async ({ page }) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();

	await page.getByRole('button', { name: /^Revoke user:alice owner/ }).click();

	// "Are you sure?" is not an answer to "how many". The dialog measures with ListUsers first.
	await expect(page.getByText(/principals? currently hold/)).toBeVisible();
	const measured = await accessState(page);
	expect(measured.listUsersBodies.at(-1)).toMatchObject({
		object: 'table:db1$t',
		relation: 'owner',
	});
	// Measured, not yet written.
	expect(measured.deleted).toHaveLength(0);

	await page.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect.poll(async () => (await accessState(page)).deleted.length).toBeGreaterThan(0);
});

test('the model stays read-only beside the graph', async ({ page }) => {
	await open(page);
	await page.getByRole('button', { name: /^Model DSL/ }).click();
	// Read-only permanently: the model lives in three checked-in files behind a CI drift gate, so a UI
	// that could mutate it would make that gate a liar.
	await expect(page.getByText('define can_read_data: reader')).toBeVisible();
	await expect(page.locator('textarea')).toHaveCount(0);
});

test('a verdict exports as committable .fga.yaml', async ({ page }) => {
	// F4's other half. A check RUN is a fact about this cluster now; the same check COMMITTED is a fact
	// CI enforces. They are not the same claim, and only the second survives the next deploy.
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();

	await expect(page.getByText('Commit as a test')).toBeVisible();
	const yaml = await page.locator('pre').filter({ hasText: 'tests:' }).first().innerText();
	// The shape must paste into model.fga.yaml unedited — same keys, same nesting.
	expect(yaml).toContain('tests:');
	expect(yaml).toContain('check:');
	expect(yaml).toContain('- user: "user:alice"');
	expect(yaml).toContain('object: "table:db1$t"');
	expect(yaml).toContain('assertions: { can_read_data: true }');

	// …and it must carry the FACTS, not just the claim. `fga model test` never contacts the OpenFGA
	// server: it evaluates the model against the fixture written in the YAML. The verdict above came
	// from the live store, so without a per-test `tuples:` block the pasted test asserts something
	// about a subject the fixture has never heard of — and fails on paste. Measured before this was
	// added: a real Dex `sub` exported alone gave `expected=true, got=false`, exit 1; with its tuples
	// the same export runs 17/17, exit 0.
	expect(yaml).toContain('tuples:');
	expect(yaml).toContain('relation: parent');
});

test('the Grant dialog simulates before it writes, and says when a grant is a no-op', async ({
	page,
}) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();

	await page.getByRole('button', { name: 'Grant' }).click();
	await page.getByLabel('Grant subject').fill('user:carol');
	await page
		.getByLabel('Grant relation')
		.selectOption('reader')
		.catch(async () => {
			await page.getByLabel('Grant relation').fill('reader');
		});
	await page.getByRole('button', { name: 'Simulate' }).click();

	// baseline=false, allowed=true → the grant genuinely unlocks access.
	await expect(page.getByText('Grants access.')).toBeVisible();
	const simulated = await accessState(page);
	expect(simulated.simulateBodies.at(-1)).toMatchObject({
		user: 'user:carol',
		relation: 'reader',
		object: 'table:db1$t',
	});
	// The hypothesis is sent; nothing is written.
	const sent = simulated.simulateBodies.at(-1);
	expect(Array.isArray(sent?.hypothetical) ? sent.hypothetical.length : 0).toBe(1);
	expect(simulated.written).toHaveLength(0);
});

// ── conditional (time-boxed) grants ──────────────────────────────────────────────────────────────────
//
// A grant that expires on its own. What these pin is that the CONDITION survives every hop the UI owns:
// offered from the model (never a hand-kept list), collected as typed inputs, sent on the write, and
// drawn differently once it is on the canvas. A break in any one degrades silently to "permanent",
// which is the failure the whole feature exists to prevent.

test('the grant dialog offers time-boxing, driven by the model', async ({ page }) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();
	await page.getByRole('button', { name: 'Grant' }).click();

	await expect(page.getByText('Time-box this grant')).toBeVisible();
	// The control appears because GET /v1/access/model SAID so. With no conditions in the model there
	// is no checkbox at all — the option is unrepresentable rather than offered and then rejected.
	await page.getByLabel('Time-box this grant').check();

	// Parameters come from the model's declared types: a duration gets presets, a timestamp a field.
	await expect(page.getByRole('button', { name: '4h', exact: true })).toBeVisible();
	await expect(page.getByText('grant_duration')).toBeVisible();
	// `current_time` is the CALLER's clock, supplied per check — it must NOT be collectable here, or the
	// window would be evaluated against a moment frozen at grant time.
	await expect(page.getByText('current_time')).toHaveCount(0);
});

test('a time-boxed grant sends its condition to the store', async ({ page }) => {
	await open(page);
	await ask(page, 'Why…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		Object: 'table:db1$t',
	});
	await expect(page.getByText('ALLOWED')).toBeVisible();
	await page.getByRole('button', { name: 'Grant' }).click();

	await page.getByLabel('Grant subject').fill('user:contractor');
	await page.getByLabel('Grant relation').selectOption('reader');
	await page.getByLabel('Time-box this grant').check();
	await page.getByRole('button', { name: '4h', exact: true }).click();
	await page.getByRole('button', { name: 'Write tuple' }).click();

	await expect.poll(async () => (await accessState(page)).written.length).toBeGreaterThan(0);
	const written = (await accessState(page)).written.at(-1);
	expect(written).toMatchObject({
		user: 'user:contractor',
		relation: 'reader',
		object: 'table:db1$t',
		condition: { name: 'non_expired_grant' },
	});
	const ctx = (written as { condition: { context: Record<string, string> } }).condition.context;
	expect(ctx.grant_duration).toBe('4h');
	// Blank timestamp means "now" — the window opens when the grant is written, which is what
	// "for the next 4 hours" means. Asserted as a real ISO instant, not merely present.
	expect(Date.parse(ctx.grant_time)).not.toBeNaN();
	expect('current_time' in ctx).toBe(false);
});

test('a conditional grant is visually distinct on the canvas', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	// The store answers with one CONDITIONAL tuple, so the canvas must not draw it as permanent.
	await expect(page.locator('[data-slot="access-node"]').first()).toBeVisible();
	const edges = page.locator('.svelte-flow__edge path.svelte-flow__edge-path');
	const dashed = page.locator('.svelte-flow__edge path[style*="stroke-dasharray"]');
	// EXISTENCE, not `toBeVisible()`. An edge between two nodes the placer put on the same row is a
	// perfectly horizontal path, whose bounding box has zero HEIGHT — and a zero-area box is exactly
	// what Playwright reports as "hidden". So the old assertion passed or failed on how many rows the
	// answer happened to have, which is why it survived until the suite's other failures stopped
	// reshuffling the parallel schedule. Nothing about the product changed; the probe was wrong.
	await expect(dashed).toHaveCount(1);
	// The half that makes it non-vacuous: the dash marks THIS grant, it is not painted on every edge.
	// (`TUPLES` carries three permanent grants beside the one time-boxed one — access-fixtures.ts:14.)
	//
	// `expect.poll`, NOT `expect(await edges.count())`. A bare `.count()` is a one-shot read with no
	// auto-waiting, so it samples whatever the canvas happens to have drawn at that instant and
	// reported 0 on a cold run — the assertion above passes because `toHaveCount` retries and this one
	// did not. Any count read outside an `expect` locator assertion needs the poll.
	await expect.poll(async () => edges.count()).toBeGreaterThan(1);
});

// ── the model/type graph ─────────────────────────────────────────────────────────────────────────────
//
// The SCHEMA, not the store. What makes this worth building rather than linking to the official
// playground is the `X from Y` edge: OpenFGA's own Types Previewer emits edges for `computedUserset`
// only and draws NO tupleToUserset at all, which in this model deletes the entire containment cascade.
// The assertions below are about exactly that.

test('the model view draws the schema, not the tuples', async ({ page }) => {
	await open(page);
	await pressView(page, 'Model schema');

	// Types and their relations, straight from the DSL. `user:alice` is a TUPLE and must not appear —
	// this view is true before a single tuple exists.
	await expect(
		page.locator('[data-slot="access-node"]').filter({ hasText: 'namespace' }).first(),
	).toBeVisible();
	await expect(page.locator('[data-slot="access-node"]').filter({ hasText: 'alice' })).toHaveCount(
		0,
	);
});

test('the model view renders `X from Y`, which the official previewer omits', async ({ page }) => {
	await open(page);
	await pressView(page, 'Model schema');
	// `define owner: [...] or owner from parent` — the cascade. Its absence is the documented defect in
	// the reference implementation, so its PRESENCE is the reason this view exists.
	await expect(page.getByText('from', { exact: true }).first()).toBeVisible();
});

test('the model view is URL-addressable and survives a reload', async ({ page }) => {
	await open(page);
	await pressView(page, 'Model schema');
	await expect.poll(() => new URL(page.url()).searchParams.get('view')).toBe('model');

	await page.goto(page.url());
	await expect(page.getByRole('heading', { name: 'Access', level: 1 })).toBeVisible({
		timeout: 20_000,
	});
	// Still the schema after a cold load — the toggle is state, not a transient click.
	await expect(page.getByRole('button', { name: 'Model schema' })).toHaveAttribute(
		'aria-pressed',
		'true',
	);
});

// ── query history ────────────────────────────────────────────────────────────────────────────────────

test('a run is remembered, re-runnable, and survives a reload', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	await expect(page.getByText(/subject\(s\) hold reader/)).toBeVisible();

	await expect(page.getByText(/^Recent queries/)).toBeVisible();
	await page.getByText(/^Recent queries/).click();
	const entry = page.getByRole('button', { name: /who can reader table:db1\$t/ });
	await expect(entry).toBeVisible();

	// Survives a cold load — the point of persisting rather than holding it in component state.
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Access', level: 1 })).toBeVisible({
		timeout: 20_000,
	});
	await page.getByText(/^Recent queries/).click();
	await expect(page.getByRole('button', { name: /who can reader table:db1\$t/ })).toBeVisible();
});

test('history de-duplicates and re-runs in one click', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	await expect(page.getByText(/subject\(s\) hold reader/)).toBeVisible();
	// The SAME question again: one entry that got more recent, not two entries.
	await page.getByRole('button', { name: 'Run' }).click();
	await page.getByText(/^Recent queries/).click();
	await expect(page.getByRole('button', { name: /who can reader table:db1\$t/ })).toHaveCount(1);

	// One click replays it — through the same hydration path a pasted link takes.
	await page.getByRole('button', { name: 'Why…' }).click();
	await page.getByRole('button', { name: /who can reader table:db1\$t/ }).click();
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('who');
});

test('history stores nothing the URL does not already carry', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	await expect(page.getByText(/subject\(s\) hold reader/)).toBeVisible();

	// This is an estate-admin surface and localStorage is readable by any script on the origin, so the
	// stored entry must be a query string and nothing else — no verdicts, no tuples, no derivations.
	const stored = await page.evaluate(() => localStorage.getItem('rask.access.history.v1'));
	expect(stored).not.toBeNull();
	const entries = JSON.parse(stored ?? '[]') as { query: string; label: string; at: number }[];
	expect(entries.length).toBeGreaterThan(0);
	for (const e of entries) {
		expect(Object.keys(e).sort()).toEqual(['at', 'label', 'query']);
		// Every key in the stored query must be one `pushUrl` writes — nothing smuggled alongside.
		for (const key of new URLSearchParams(e.query).keys()) {
			expect([
				'q',
				'user',
				'relation',
				'object',
				'type',
				'depth',
				'seed',
				'types',
				'rels',
				'view',
				'run',
			]).toContain(key);
		}
	}
});

// ── live updates ─────────────────────────────────────────────────────────────────────────────────────

test('a grant landing elsewhere on the estate redraws the canvas, no reload, no click', async ({
	page,
}) => {
	await open(page);
	// The resting canvas holds the fixture store; the subject about to be granted is not on it.
	await expect(page.locator('[data-slot="access-node"]').first()).toBeVisible();
	await expect(
		page.locator('[data-slot="access-node"]').filter({ hasText: 'live-wire' }),
	).toHaveCount(0);

	// Wait for the control cursor to have SETTLED: its first successful probe is the baseline, not a
	// move (feeds.remote.ts), so an event injected before that probe lands would be silently absorbed
	// and this spec would time out flakily instead of testing the tick.
	await expect.poll(async () => (await accessState(page)).eventProbes).toBeGreaterThan(0);

	// Another admin grants somewhere else: the tuple lands in the store, and — because access_admin now
	// emits `grant_added` on every raw-tuple write — the control cursor moves. Two mock calls model the
	// two halves of that one real event.
	await page.request.post(`${MOCK_CATALOG}/__mock/access/tuple`, {
		data: {
			bearer: token,
			tuple: { user: 'user:live-wire', relation: 'reader', object: 'table:db1$t' },
		},
	});
	// `bearer` scopes the event to THIS test's bucket: the canvas's own control cursor ticks, and no
	// other spec's feed ever sees the event (control-events.spec.ts asserts exact feed contents).
	await page.request.post(`${MOCK_CATALOG}/__mock/add`, {
		data: { bearer: token, action: 'grant_added', object_type: 'grant', object_id: 'table:db1$t' },
	});

	// Within one control-cursor poll the canvas re-reads the store and the new grant simply appears.
	// 15s: the `query.live` probe runs every 5s (POLL_MS) and the re-read follows it.
	await expect(
		page.locator('[data-slot="access-node"]').filter({ hasText: 'live-wire' }),
	).toBeVisible({ timeout: 15_000 });
});
