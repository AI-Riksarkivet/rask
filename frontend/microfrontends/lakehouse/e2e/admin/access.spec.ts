import { expect, test } from '@playwright/test';
import { mockMe, signIn } from './session';

// `/lakehouse/admin/access` — the FGA workbench (goal cond 4) against the frozen /v1/access contract, through
// the zone's thin bearer-forwarding /capi routes. Hermetic via page.route: the four tabs (Graph /
// Tuples / Check / Model) each drive their wire calls and the mock pins the REQUEST shapes — method,
// path, query filters and JSON bodies are the contract.

const TUPLES = [
	{ user: 'user:alice', relation: 'owner', object: 'table:db1$t' },
	{ user: 'user:bob', relation: 'reader', object: 'table:db1$t' },
	{ user: 'namespace:db1', relation: 'parent', object: 'table:db1$t' },
];

type Tuple = { user: string; relation: string; object: string };

let written: Tuple | null;
let deleted: Tuple | null;
let checked: Tuple | null;
let tupleQueries: string[];

test.beforeEach(async ({ context, page }) => {
	await signIn(context);
	await mockMe(page); // estate-admin identity: the admin layout door opens
	written = null;
	deleted = null;
	checked = null;
	tupleQueries = [];
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const url = new URL(req.url());
		const path = url.pathname.replace(/^.*\/capi/, '');
		// The layout's identity fetch shares this glob and the handler REGISTERED LAST wins —
		// fall back to the mockMe route instead of 404ing the door shut.
		if (path === '/v1/me') return route.fallback();
		if (path === '/v1/table')
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ tables: ['db1$t'] }),
			});
		if (path === '/v1/access/tuples' && req.method() === 'GET') {
			tupleQueries.push(url.search);
			const object = url.searchParams.get('object');
			const user = url.searchParams.get('user');
			const objectType = url.searchParams.get('object_type');
			let tuples = TUPLES.concat(written ? [written] : []).filter(
				(t) => !deleted || JSON.stringify(t) !== JSON.stringify(deleted),
			);
			if (object) tuples = tuples.filter((t) => t.object === object);
			if (user) tuples = tuples.filter((t) => t.user === user);
			if (objectType) tuples = tuples.filter((t) => t.object.startsWith(`${objectType}:`));
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ tuples, continuation: null }),
			});
		}
		if (path === '/v1/access/tuples' && req.method() === 'POST') {
			written = req.postDataJSON() as Tuple;
			return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
		}
		if (path === '/v1/access/tuples' && req.method() === 'DELETE') {
			deleted = req.postDataJSON() as Tuple;
			return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
		}
		if (path === '/v1/access/check' && req.method() === 'POST') {
			checked = req.postDataJSON() as Tuple;
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ allowed: checked.user === 'user:alice', checked }),
			});
		}
		if (path === '/v1/access/model' && req.method() === 'GET')
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					dsl: 'model\n  schema 1.1\n\ntype user\n\ntype table\n  relations\n    define owner: [user]',
					authorization_model_id: '01JTESTMODEL',
				}),
			});
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: '{"detail":"unstubbed"}',
		});
	});
});

test('the Graph tab seeds from any object id and re-seeds from a neighbor click', async ({
	page,
}) => {
	await page.goto('/lakehouse/admin/access');
	// The registry chips offer the estate's tables as one-click seeds.
	await page.getByRole('button', { name: 'db1$t', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'table:db1$t' })).toBeVisible();
	// One hop off the tuple store: both grantees, the namespace container, the relation labels.
	const flow = page.locator('.svelte-flow');
	await expect(flow).toContainText('alice');
	await expect(flow).toContainText('bob');
	await expect(flow).toContainText('db1'); // the parent namespace node
	await expect(flow).toContainText('owner');
	// The wire calls are the contract: inbound is one {object} read; outbound fans out one
	// {user, object_type} read per model type — the API rejects a bare user filter by design.
	expect(tupleQueries.some((q) => q.includes('object=table%3Adb1%24t'))).toBe(true);
	expect(
		tupleQueries.some((q) => q.includes('user=table%3Adb1%24t') && q.includes('object_type=table')),
	).toBe(true);
	expect(
		tupleQueries.some((q) => q.includes('user=table%3Adb1%24t') && q.includes('object_type=user')),
	).toBe(true);
	expect(tupleQueries.every((q) => !q.includes('user=') || q.includes('object_type='))).toBe(true);
	// A free-typed seed works too — the workbench is not table-scoped.
	await page.getByLabel('Graph seed object').fill('user:alice');
	await page.getByRole('button', { name: 'Seed', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'user:alice', exact: true })).toBeVisible();
	await expect
		.poll(() =>
			tupleQueries.some((q) => q.includes('user=user%3Aalice') && q.includes('object_type=')),
		)
		.toBe(true);
});

test('the Tuples tab lists, filters server-side, grants via the dialog and revokes behind a confirm', async ({
	page,
}) => {
	await page.goto('/lakehouse/admin/access');
	await page.getByRole('tab', { name: 'Tuples' }).click();
	const table = page.locator('table');
	await expect(table).toContainText('user:alice');
	await expect(table).toContainText('namespace:db1');
	// Server-side filter, applied on Search ONLY: typing must not fire an estate-gated FGA read
	// (and an audit event) per keystroke.
	const before = tupleQueries.length;
	await page.getByLabel('User filter').pressSequentially('user:bob');
	// Scoped to the FORM: the navbar's media trigger is also named Search since the zone rename, so a
	// page-wide role query matches two buttons and trips strict mode.
	const searchBtn = page.locator('form').getByRole('button', { name: 'Search', exact: true });
	// A bare user filter is rejected by the API by design — the form refuses to send one.
	await expect(searchBtn).toBeDisabled();
	await expect(page.getByText('needs an object_type or object')).toBeVisible();
	expect(tupleQueries.length).toBe(before);
	await page.getByLabel('Object type filter').fill('table');
	await searchBtn.click();
	await expect
		.poll(() =>
			tupleQueries.some((q) => q.includes('user=user%3Abob') && q.includes('object_type=table')),
		)
		.toBe(true);
	expect(tupleQueries.length).toBe(before + 1); // exactly the one Search-triggered read
	await expect(table).not.toContainText('user:alice');
	// Grant: the dialog POSTs the exact tuple body.
	await page.getByLabel('User filter').clear();
	await page.getByRole('button', { name: 'Grant', exact: true }).click();
	await page.getByLabel('Grant user').fill('user:carol');
	await page.getByLabel('Grant relation').fill('writer');
	await page.getByLabel('Grant object').fill('table:db1$t');
	await page.getByRole('button', { name: 'Write tuple', exact: true }).click();
	await expect
		.poll(() => written)
		.toEqual({ user: 'user:carol', relation: 'writer', object: 'table:db1$t' });
	await expect(table).toContainText('user:carol'); // the reload reflects the write
	// Revoke: the row action confirms, then DELETEs the same tuple shape.
	await page.getByLabel('Revoke user:bob reader table:db1$t').click();
	await page.getByRole('button', { name: 'Revoke', exact: true }).last().click();
	await expect
		.poll(() => deleted)
		.toEqual({ user: 'user:bob', relation: 'reader', object: 'table:db1$t' });
	await expect(table).not.toContainText('user:bob');
});

test('the Check tab renders the big verdict and the echoed triple', async ({ page }) => {
	await page.goto('/lakehouse/admin/access');
	await page.getByRole('tab', { name: 'Check' }).click();
	await page.getByLabel('Check user').fill('user:alice');
	await page.getByLabel('Check relation').fill('can_read_data');
	await page.getByLabel('Check object').fill('table:db1$t');
	await page.getByRole('button', { name: 'Check', exact: true }).click();
	await expect
		.poll(() => checked)
		.toEqual({ user: 'user:alice', relation: 'can_read_data', object: 'table:db1$t' });
	await expect(page.locator('.verdict')).toContainText('ALLOWED');
	// The triple rendered is the catalog's ECHO, not local form state.
	await expect(page.getByLabel('Checked triple')).toContainText(
		'(user:alice, can_read_data, table:db1$t)',
	);
	// And a denied probe flips the verdict.
	await page.getByLabel('Check user').fill('user:mallory');
	await page.getByRole('button', { name: 'Check', exact: true }).click();
	await expect(page.locator('.verdict')).toContainText('DENIED');
});

test('the Model tab shows the read-only DSL with the code-migration note', async ({ page }) => {
	await page.goto('/lakehouse/admin/access');
	await page.getByRole('tab', { name: 'Model' }).click();
	await expect(page.getByText('Model changes are code migrations.')).toBeVisible();
	const dsl = page.getByLabel('Authorization model DSL');
	await expect(dsl).toContainText('schema 1.1');
	await expect(dsl).toContainText('define owner: [user]');
	await expect(page.getByText('authorization_model_id: 01JTESTMODEL')).toBeVisible();
	// Read-only: no edit affordance exists on this tab.
	await expect(page.locator('.model textarea, .model input')).toHaveCount(0);
});
