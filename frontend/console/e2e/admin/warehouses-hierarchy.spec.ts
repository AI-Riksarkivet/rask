import { test, expect, type Page, type Route } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// The hierarchy drill-down (goal cond 3): project → warehouse → namespace → table, driven with an
// alice-shaped /management/v1/me (`page.route`d). The warehouse and registry reads are seeded on the
// mock catalog per bearer.
//
// The table-DETAIL rung of the old file stayed in `e2e/catalog/hierarchy.spec.ts`: its aggregate route
// is a different area's migration.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const ME_ALICE = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	can_create_project: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

const PROJECTS = [
	{
		project: 'acme',
		warehouses: [{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' }],
		admins: ['alice'],
	},
];

const WAREHOUSE = {
	id: 'acme-wh',
	project: 'acme',
	bucket: 'acme-bucket',
	root_uri: 's3://acme-bucket',
	status: 'active',
};

const TABLES = ['acme-wh$silver$features', 'acme-wh$gold$catalog', 'other-wh$bronze$events'];

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await page.route('**/api/catalog/management/v1/me', (route) => json(route, ME_ALICE));
	await seed(page, {
		'GET /management/v1/projects': PROJECTS,
		// No `GET /management/v1/projects/acme` seed: the only page that read it was `/catalog/projects/<p>`,
		// which is deleted (see the drill test below).
		// Wrapped in the seed's explicit {status, body} form ON PURPOSE: a bare object carrying its own
		// `status` key — which every warehouse record does — is otherwise read as that form, and the
		// record's `"active"` becomes the HTTP status.
		'GET /management/v1/warehouses/acme-wh': { status: 200, body: WAREHOUSE },
		// The warehouse's own listing (the spec's ListNamespaces on its name) answers the NAMES of the
		// namespaces under it — the list the page renders, an empty one included (#66).
		'GET /v1/namespace/acme-wh/list': { namespaces: ['silver', 'gold', 'raw'] },
		// A namespace nests: silver holds `deep`, so the drill-down must lead there too.
		'GET /v1/namespace/acme-wh%24silver/list': { namespaces: ['deep'] },
		'GET /v1/table': { tables: TABLES },
	});
});

/** The bodies this test's bearer POSTed to one path, as the mock recorded them. */
const postsTo = async (page: Page, path: string): Promise<unknown[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	const { calls } = (await res.json()) as {
		calls: { method: string; path: string; body: unknown }[];
	};
	return calls.filter((c) => c.method === 'POST' && c.path === path).map((c) => c.body);
};

test('a warehouse grant manager reviews, grants and revokes access on the warehouse', async ({
	page,
}) => {
	const grant = { object: 'warehouse:acme-wh', user: 'dex~svc', relation: 'decider' };
	await seed(page, {
		'POST /management/v1/warehouse/acme-wh/access/list': {
			object: 'warehouse:acme-wh',
			grants: [{ relation: 'can_read_assignments', users: ['user:alice'] }],
		},
		'POST /management/v1/warehouse/acme-wh/access/my-permissions': {
			object: 'warehouse:acme-wh',
			subject: 'user:alice',
			permissions: { can_grant_decider: true, can_revoke_grant: true },
		},
		'POST /management/v1/warehouse/acme-wh/access/grant': { ...grant, granted: true },
		'POST /management/v1/warehouse/acme-wh/access/revoke': { ...grant, granted: false },
	});
	await page.goto('/catalog/warehouses/acme-wh');
	const access = page.locator('section', { has: page.getByRole('heading', { name: 'Access' }) });
	await access.getByRole('button', { name: 'Access review' }).click();
	await expect(access.getByRole('row').filter({ hasText: 'can_read_assignments' })).toContainText(
		'user:alice',
	);
	// A warehouse has no check door, so no simulator is offered.
	await expect(access.getByText('Check a specific access')).toHaveCount(0);

	await access.getByLabel('Subject to grant to').fill('dex~svc');
	await access.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: /^decider/ }).click();
	await access.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(access.locator('.verdict.ok')).toHaveText('decider granted to dex~svc.');
	expect(await postsTo(page, '/management/v1/warehouse/acme-wh/access/grant')).toEqual([
		{ user: 'dex~svc', relation: 'decider' },
	]);

	await access.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect(access.locator('.verdict.ok')).toHaveText('decider revoked from dex~svc.');
	expect(await postsTo(page, '/management/v1/warehouse/acme-wh/access/revoke')).toEqual([
		{ user: 'dex~svc', relation: 'decider' },
	]);
});

test.describe('the warehouse trash', () => {
	// Every moment on the page prints in the browser's zone.
	test.use({ timezoneId: 'UTC' });

	const DROPPED_NAMESPACE = {
		kind: 'namespace',
		id: 'acme-wh$staging',
		uuid: '5e883388-5ab8-46ff-b835-7d29bf4a72f6',
		location: 's3://acme-bucket/staging',
		dropped_by: 'user:alice',
		dropped_at: '2026-11-20T08:00:00Z',
		expires_at: '2026-11-27T08:00:00Z',
		expired: false,
	};
	const DROPPED_TABLE = {
		kind: 'table',
		id: 'acme-wh$silver$old',
		uuid: '0b1f9c2e-0000-4000-8000-000000000002',
		location: 's3://acme-bucket/silver/old.lance',
		dropped_by: 'user:alice',
		dropped_at: '2026-11-21T09:30:00Z',
		expires_at: '2026-11-28T09:30:00Z',
		expired: false,
	};
	const TRASH = 'GET /management/v1/warehouses/acme-wh/trash';

	/** Every undrop this test's bearer sent, as the path and query the mock recorded. */
	const undrops = async (page: Page): Promise<string[]> => {
		const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
			headers: { authorization: `Bearer ${token}` },
		});
		const { calls } = (await res.json()) as { calls: { method: string; path: string }[] };
		return calls
			.filter((c) => c.method === 'POST' && c.path.includes('/undrop'))
			.map((c) => c.path);
	};

	test('lists what the warehouse dropped by path and deadline, and restores a namespace from it', async ({
		page,
	}) => {
		await seed(page, {
			[TRASH]: { objects: [DROPPED_NAMESPACE, DROPPED_TABLE] },
			[`POST /management/v1/namespace/acme-wh%24staging/undrop?uuid=${DROPPED_NAMESPACE.uuid}`]: {
				properties: {},
			},
		});
		await page.goto('/catalog/warehouses/acme-wh');
		const trash = page.locator('section', { has: page.getByRole('heading', { name: 'Trash' }) });
		const namespaceRow = trash.getByRole('row').filter({ hasText: 'acme-wh › staging' });
		await expect(namespaceRow).toContainText('namespace');
		await expect(namespaceRow).toContainText('2026-11-20 08:00:00 UTC');
		await expect(namespaceRow).toContainText('2026-11-27 08:00:00 UTC');
		const tableRow = trash.getByRole('row').filter({ hasText: 'acme-wh › silver › old' });
		await expect(tableRow).toContainText('table');
		await expect(tableRow).toContainText('2026-11-28 09:30:00 UTC');
		await expect(page.locator('a.row', { hasText: /^staging/ })).toHaveCount(0);

		// What the catalog answers once the namespace is back: out of the trash, in the listing.
		await seed(page, {
			[TRASH]: { objects: [DROPPED_TABLE] },
			'GET /v1/namespace/acme-wh/list': { namespaces: ['silver', 'gold', 'raw', 'staging'] },
		});
		await trash.getByRole('button', { name: 'Undrop acme-wh › staging' }).click();

		await expect(namespaceRow).toHaveCount(0);
		await expect(tableRow).toBeVisible();
		await expect(page.locator('a.row', { hasText: /^staging/ })).toBeVisible();
		expect(await undrops(page)).toEqual([
			`/management/v1/namespace/acme-wh%24staging/undrop?uuid=${DROPPED_NAMESPACE.uuid}`,
		]);
	});

	test('a refused undrop keeps the entry and shows the catalog’s detail beside that entry alone', async ({
		page,
	}) => {
		const detail = 'a table holds the name acme-wh$silver$old: restore it under another name';
		// Two drops of one name whose records name no uuid, as the catalog answers a record that has none:
		// nothing but their place in the listing tells them apart.
		const earlier = { ...DROPPED_TABLE, uuid: '', dropped_at: '2026-11-19T09:30:00Z' };
		const later = { ...DROPPED_TABLE, uuid: '' };
		await seed(page, {
			[TRASH]: { objects: [earlier, later] },
			'POST /management/v1/table/acme-wh%24silver%24old/undrop': {
				status: 409,
				body: {
					type: 'https://lance.org/problems/tablealreadyexistserror',
					title: 'TableAlreadyExistsError',
					status: 409,
					code: 5,
					detail,
				},
			},
		});
		await page.goto('/catalog/warehouses/acme-wh');
		const trash = page.locator('section', { has: page.getByRole('heading', { name: 'Trash' }) });
		const rows = trash.getByRole('row').filter({ hasText: 'acme-wh › silver › old' });
		await expect(rows).toHaveCount(2);
		await rows
			.first()
			.getByRole('button', { name: /^Undrop acme-wh › silver › old/ })
			.click();

		await expect(rows.first().getByRole('alert')).toHaveText(detail);
		await expect(trash.getByRole('alert')).toHaveCount(1);
		await expect(rows).toHaveCount(2);
		expect(await undrops(page)).toEqual(['/management/v1/table/acme-wh%24silver%24old/undrop']);
	});
});

test('drills warehouse → namespace → table, and the breadcrumb leads back up', async ({ page }) => {
	// STARTS AT THE WAREHOUSE: a project is the TOP of the hierarchy (project › warehouse › namespace ›
	// table) and its page is `/projects/<p>`; the up-link back to it is pinned below and in
	// warehouses-drawers.spec.ts.
	await page.goto('/catalog/warehouses/acme-wh');

	// The warehouse rung: registry facts + the namespaces its listing names, each by its own name; a
	// namespace in another warehouse must NOT appear.
	await expect(page).toHaveURL(/\/catalog\/warehouses\/acme-wh$/);
	await expect(page.getByText('s3://acme-bucket')).toBeVisible();
	// The way back UP: the project's own page.
	const up = page.getByRole('link', { name: 'acme', exact: true });
	await expect(up).toHaveAttribute('href', '/projects/acme');
	const silverRow = page.locator('a.row', { hasText: /^silver/ });
	await expect(silverRow).toBeVisible();
	await expect(page.locator('a.row', { hasText: /^gold/ })).toBeVisible();
	// An empty namespace is listed with no tables, not left out.
	await expect(page.locator('a.row', { hasText: /^raw/ })).toContainText('0 tables');
	await expect(page.locator('a.row', { hasText: 'bronze' })).toHaveCount(0);
	await silverRow.click();

	// The namespace rung: its child namespace and its member table, each by its own name.
	await expect(page).toHaveURL(/\/catalog\/namespaces\/acme-wh%24silver$/);
	await expect(page.getByRole('link', { name: 'deep', exact: true })).toHaveAttribute(
		'href',
		'/catalog/namespaces/acme-wh%24silver%24deep',
	);
	await page.getByRole('link', { name: 'features', exact: true }).click();

	// The table, named by its path, and its breadcrumb back through its namespace and warehouse.
	await expect(page).toHaveURL(/\/catalog\/tables\/acme-wh%24silver%24features$/);
	await expect(page.getByRole('heading', { level: 1 })).toHaveText('acme-wh › silver › features');
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' });
	await expect(crumbs.getByRole('link', { name: 'silver', exact: true })).toHaveAttribute(
		'href',
		'/catalog/namespaces/acme-wh%24silver',
	);
	await expect(crumbs.getByRole('link', { name: 'acme-wh', exact: true })).toHaveAttribute(
		'href',
		'/catalog/warehouses/acme-wh',
	);
});

// DELETED WITH ITS SURFACE, and the gap is named rather than quietly closed: this file used to end
// with 'a member without the estate privilege still gets their membership gallery', which drove the
// projects LIST page — a 403 from /management/v1/projects had to degrade to the caller's own memberships from
// /management/v1/me rather than 500. That page is gone by the ruling above, and the estate's project gallery is
// the console's landing (`home/src/routes/+page.svelte`, which already branches on
// `data.estateAdmin` for exactly this reason).
//
// The BEHAVIOUR still matters and is currently UNTESTED: home's e2e suite runs auth-off with no
// catalog stand-in, so there is nowhere in it to seed a 403 today. Covering it needs home to gain the
// mock-upstream pattern the lakehouse suite uses. Named here so the deletion reads as a
// move with a debt, not as coverage that silently evaporated.
