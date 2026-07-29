import { expect, test } from '@playwright/test';
import { mockMe, signIn } from './session';

// `/lakehouse/governance/access` — the ONE query-driven FGA explorer.
//
// This replaced four tabs (Graph / Tuples / Check / Model) that shared no state and no URL, and the
// spec replaced with it: what is exercised now is the WORKFLOW those tabs made impossible — ask a
// question, see the answer lit on a canvas that never unmounts, narrow it without refetching, and read
// the selected node beside it rather than instead of it.
//
// Hermetic via page.route. The mock pins REQUEST shapes (method, path, JSON body) because those are the
// frozen /v1/access contract, and it asserts the derivation is rendered from `expand`, not inferred.

const TUPLES = [
	{ user: 'user:alice', relation: 'owner', object: 'table:db1$t' },
	{ user: 'user:bob', relation: 'reader', object: 'table:db1$t' },
	{ user: 'namespace:db1', relation: 'parent', object: 'table:db1$t' },
];

const DSL = `model
  schema 1.1

type user

type namespace
  relations
    define parent: [warehouse, namespace]
    define owner: [user, role#assignee] or owner from parent
    define reader: [user, role#assignee] or owner

type table
  relations
    define parent: [namespace]
    define owner: [user, role#assignee] or owner from parent
    define reader: [user, role#assignee] or owner
    define can_read_data: reader
`;

const leaf = (over: Record<string, unknown> = {}) => ({
	users: null,
	computed: null,
	tuple_to_userset: null,
	expanded: null,
	continues: null,
	...over,
});

const node = (name: string, over: Record<string, unknown> = {}) => ({
	name,
	leaf: null,
	union: null,
	intersection: null,
	difference: null,
	truncated: null,
	cycle: null,
	...over,
});

// The shape `fga.expand_tree` returns at depth>1: a `tuple_to_userset` leaf whose `expanded` child is
// the PARENT object's own tree. This is the thing a single Expand cannot give and a tuple table can
// never show — the spec exists to prove it reaches the screen.
const EXPAND_TREE = node('table:db1$t#can_read_data', {
	union: [
		node('from-parent', {
			leaf: leaf({
				tuple_to_userset: { tupleset: 'table:db1$t#parent', computed: ['owner'] },
				expanded: [node('namespace:db1#owner', { leaf: leaf({ users: ['user:alice'] }) })],
			}),
		}),
	],
});

type Tuple = { user: string; relation: string; object: string };
type Body = Record<string, unknown>;

let expandBody: Body | null;
let listUsersBodies: Body[];
let listObjectsBody: Body | null;
let deleted: Tuple | null;

test.beforeEach(async ({ context, page }) => {
	await signIn(context);
	await mockMe(page); // estate-admin identity: the governance layout door opens
	expandBody = null;
	listUsersBodies = [];
	listObjectsBody = null;
	deleted = null;

	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const url = new URL(req.url());
		const path = url.pathname.replace(/^.*\/capi/, '');
		const json = (body: unknown) =>
			route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

		// The layout's identity fetch shares this glob and the handler REGISTERED LAST wins — fall back
		// to the mockMe route instead of 404ing the door shut.
		if (path === '/v1/me') return route.fallback();
		if (path === '/v1/table') return json({ tables: ['db1$t'] });
		if (path === '/v1/access/model') return json({ dsl: DSL, authorization_model_id: '01MODEL' });

		if (path === '/v1/access/tuples' && req.method() === 'GET') {
			const object = url.searchParams.get('object');
			let tuples = TUPLES.filter((t) => !deleted || JSON.stringify(t) !== JSON.stringify(deleted));
			if (object) tuples = tuples.filter((t) => t.object === object);
			return json({ tuples, continuation: null });
		}
		if (path === '/v1/access/tuples' && req.method() === 'DELETE') {
			deleted = req.postDataJSON() as Tuple;
			return json(deleted);
		}
		if (path === '/v1/access/check') {
			const body = req.postDataJSON() as Tuple;
			return json({ allowed: true, checked: { ...body, user: 'user:alice' } });
		}
		if (path === '/v1/access/expand') {
			expandBody = req.postDataJSON() as Body;
			return json({
				tree: EXPAND_TREE,
				object: 'table:db1$t',
				relation: 'can_read_data',
				depth: 3,
			});
		}
		if (path === '/v1/access/list-users') {
			listUsersBodies.push(req.postDataJSON() as Body);
			return json({
				users: ['user:alice', 'user:bob', 'role:validators#assignee'],
				object: 'table:db1$t',
				relation: 'reader',
				user_type: 'user',
				user_relation: null,
				truncated: false,
			});
		}
		if (path === '/v1/access/list-objects') {
			listObjectsBody = req.postDataJSON() as Body;
			return json({
				objects: ['table:db1$t', 'table:db1$u'],
				user: 'user:alice',
				relation: 'can_read_data',
				type: 'table',
			});
		}
		return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
	});
});

const open = async (page: import('@playwright/test').Page) => {
	await page.goto('/lakehouse/governance/access');
	await expect(page.getByRole('heading', { name: 'Access', level: 1 })).toBeVisible();
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
	expect(expandBody).toMatchObject({ object: 'table:db1$t', relation: 'can_read_data', depth: 3 });
});

test('"who can" answers from ListUsers, usersets included', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });

	await expect(page.getByText('3 subject(s) hold reader')).toBeVisible();
	// The userset arm — `role:validators#assignee` — is the one the old wrapper discarded outright.
	await expect(
		page.locator('[data-slot="access-node"]').filter({ hasText: 'validators' }),
	).toBeVisible();
	expect(listUsersBodies[0]).toMatchObject({ object: 'table:db1$t', relation: 'reader' });
});

test('"what can" answers from ListObjects', async ({ page }) => {
	await open(page);
	await ask(page, 'What can…', {
		Subject: 'user:alice',
		Relation: 'can_read_data',
		'Object type': 'table',
	});

	await expect(page.getByText('holds can_read_data on 2 table object(s)')).toBeVisible();
	expect(listObjectsBody).toMatchObject({
		user: 'user:alice',
		relation: 'can_read_data',
		type: 'table',
	});
});

test('the facet rail filters what is already rendered — no refetch', async ({ page }) => {
	await open(page);
	await ask(page, 'Who can…', { Relation: 'reader', Object: 'table:db1$t' });
	await expect(page.getByText('3 subject(s) hold reader')).toBeVisible();

	const before = listUsersBodies.length;
	const nodesBefore = await page.locator('[data-slot="access-node"]').count();

	// Turning a type facet on narrows the canvas…
	await page.getByLabel('Filter by type role').click();
	await expect(page.getByRole('button', { name: 'Clear filters' })).toBeVisible();
	await expect
		.poll(async () => page.locator('[data-slot="access-node"]').count())
		.toBeLessThan(nodesBefore);

	// …and costs ZERO further requests. Filtering that refetches is a different feature, and a much
	// slower one, than filtering what you are already looking at.
	expect(listUsersBodies.length).toBe(before);
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
	expect(listUsersBodies.at(-1)).toMatchObject({ object: 'table:db1$t', relation: 'owner' });
	// Measured, not yet written.
	expect(deleted).toBeNull();

	await page.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect.poll(() => deleted).not.toBeNull();
});

test('the model stays read-only beside the graph', async ({ page }) => {
	await open(page);
	await page.getByRole('button', { name: /^Model DSL/ }).click();
	// Read-only permanently: the model lives in three checked-in files behind a CI drift gate, so a UI
	// that could mutate it would make that gate a liar.
	await expect(page.getByText('define can_read_data: reader')).toBeVisible();
	await expect(page.locator('textarea')).toHaveCount(0);
});
