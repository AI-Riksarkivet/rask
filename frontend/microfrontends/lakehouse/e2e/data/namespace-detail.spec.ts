import { test, expect, type Route } from '@playwright/test';

// Hermetic /namespaces/<id> coverage (sweep group 3): the namespace detail page surfaces the
// catalog's namespace-scoped governance — the table roster (from the same /v1/table list the
// registry groups), the #50 maintenance-policy card (describe/set/delete via the namespace policy
// BFF routes), and the kind-generalized GrantsPanel (list/grant/revoke against
// /capi/v1/namespace/<id>/access/*). Mock the /capi calls; assert the writes carry the exact wire
// bodies and land on the NAMESPACE paths (not the table ones).

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const TABLES = { tables: ['bronze$events', 'gold$catalog', 'gold$metrics'] };

const POLICY = {
	kind: 'namespace',
	id: 'gold',
	path: 'lance-catalog/gold',
	compact_enabled: true,
	retention_days: 14,
	retain_versions: 5,
};

const ACL = {
	object: 'namespace:gold',
	grants: [
		{ relation: 'can_read_data', users: ['user:alice'] },
		{ relation: 'can_delete', users: ['user:alice'] },
	],
};

let grantPost: { path: string; body: unknown } | null;
let policyPut: { path: string; body: unknown } | null;
let policyDeleted: string | null;

test.beforeEach(async ({ page }) => {
	grantPost = null;
	policyPut = null;
	policyDeleted = null;
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/table') return json(route, TABLES);
		if (path === '/v1/namespace/gold/policy/describe') return json(route, POLICY);
		if (path === '/v1/namespace/gold/policy' && req.method() === 'PUT') {
			policyPut = { path, body: req.postDataJSON() as unknown };
			return json(route, { ...POLICY, ...(policyPut.body as object) });
		}
		if (path === '/v1/namespace/gold/policy' && req.method() === 'DELETE') {
			policyDeleted = path;
			return json(route, { status: 'deleted', kind: 'namespace', id: 'gold' });
		}
		if (path === '/v1/namespace/gold/access/list') return json(route, ACL);
		if (path === '/v1/namespace/gold/access/grant') {
			const body = req.postDataJSON() as { user: string; relation: string };
			grantPost = { path, body };
			return json(route, {
				object: 'namespace:gold',
				user: `user:${body.user}`,
				relation: body.relation,
				granted: true,
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('renders the header, table roster, and policy chips from the mocked list + policy', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces/gold');
	await expect(page.getByRole('heading', { name: 'gold', exact: true })).toBeVisible();
	// The count comes from the registry list, grouped by the `<namespace>$` prefix. Scoped to the page's
	// own header: the AppShell topbar and the access-graph card both contribute <header> elements too.
	await expect(page.locator('.page > header')).toContainText('2 tables');
	await expect(page.locator('a.row', { hasText: 'gold$catalog' })).toHaveAttribute(
		'href',
		'/lakehouse/data/tables/gold%24catalog',
	);
	// bronze's table must NOT bleed into gold's roster.
	await expect(page.locator('a.row', { hasText: 'bronze$events' })).toHaveCount(0);
	// The #50 policy card renders the described policy as chips.
	await expect(page.locator('.chip', { hasText: 'retention 14d' })).toBeVisible();
	await expect(page.locator('.chip', { hasText: 'keep last 5' })).toBeVisible();
});

test('a grant POSTs the exact {user, relation} body to the NAMESPACE access route', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(page.locator('table.acl')).toContainText('can_delete');
	await page.getByPlaceholder('user (e.g. alice), or role:… / team:…#member').last().fill('bob');
	// The manage form's rung picker is the @rask/ui Select (bits-ui) — open by aria-label, click option.
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'reader', exact: true }).click();
	await page.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(page.locator('.verdict.allow')).toContainText('granted to');
	// Poll (don't race the route interception) — and pin the PATH so a regression that silently
	// falls back to the table surface can't pass.
	await expect
		.poll(() => grantPost)
		.toEqual({
			path: '/v1/namespace/gold/access/grant',
			body: { user: 'bob', relation: 'reader' },
		});
});

test('editing the policy PUTs the drafted bounds to the namespace policy route', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces/gold');
	await page.getByRole('button', { name: 'Edit', exact: true }).click();
	await page.getByLabel('retention days').fill('30');
	await page.getByRole('button', { name: 'Save policy' }).click();
	await expect
		.poll(() => policyPut)
		.toEqual({
			path: '/v1/namespace/gold/policy',
			body: { compact_enabled: true, retention_days: 30, retain_versions: 5 },
		});
	// The card re-renders from the write's response — latest state, no stale chips.
	await expect(page.locator('.chip', { hasText: 'retention 30d' })).toBeVisible();
});

test('removing the policy DELETEs it and the card falls back to the global-defaults state', async ({
	page,
}) => {
	await page.goto('/lakehouse/data/namespaces/gold');
	await page.getByRole('button', { name: 'Remove', exact: true }).click();
	await expect.poll(() => policyDeleted).toBe('/v1/namespace/gold/policy');
	await expect(page.getByText('No policy — the sweep applies the global defaults.')).toBeVisible();
});

test('a 403 access review renders the denial state, never the ACL', async ({ page }) => {
	// A later page.route wins over the beforeEach glob — deny the review like the catalog's FGA gate
	// (owner-tier can_delete on the namespace) does for a non-owner.
	await page.route('**/capi/v1/namespace/gold/access/list', (route) =>
		json(route, { detail: 'forbidden' }, 403),
	);
	await page.goto('/lakehouse/data/namespaces/gold');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(
		page.getByText('Owner access required to review who can reach this namespace.'),
	).toBeVisible();
	await expect(page.locator('table.acl')).toHaveCount(0);
});

test('a 403 policy write surfaces the owner-rung denial on the card', async ({ page }) => {
	await page.route('**/capi/v1/namespace/gold/policy', (route) =>
		json(route, { detail: 'forbidden' }, 403),
	);
	await page.goto('/lakehouse/data/namespaces/gold');
	await page.getByRole('button', { name: 'Edit', exact: true }).click();
	await page.getByRole('button', { name: 'Save policy' }).click();
	await expect(
		page.getByText('Denied: policy changes need the owner rung (can_delete).'),
	).toBeVisible();
});

test('the registry links each namespace name to its detail page', async ({ page }) => {
	await page.goto('/lakehouse/data/namespaces');
	await expect(page.locator('a.ns-name', { hasText: 'gold' })).toHaveAttribute(
		'href',
		'/lakehouse/data/namespaces/gold',
	);
	await page.locator('a.ns-name', { hasText: 'gold' }).click();
	await expect(page).toHaveURL(/\/data\/namespaces\/gold$/);
	await expect(page.getByRole('heading', { name: 'gold', exact: true })).toBeVisible();
});
