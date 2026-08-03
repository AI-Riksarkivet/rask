import { test, expect, type Page, type Route } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// The hierarchy drill-down (goal cond 3): project → warehouse → namespace → table, driven with an
// alice-shaped /v1/me. The project/warehouse/registry reads ride `warehouses.remote.ts` and
// `catalog.remote.ts` now — they run on the ZONE SERVER, so they are seeded on the mock catalog per
// bearer rather than intercepted at the browser boundary. `/capi/v1/me` is the one read still crossing
// the browser (the OIDC/BFF identity plane is deliberately kept there), so it stays `page.route`d.
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

const TABLES = ['acme-silver$features', 'acme-gold$catalog', 'bronze$events'];

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await page.route('**/capi/v1/me', (route) => json(route, ME_ALICE));
	await seed(page, {
		'GET /v1/projects': PROJECTS,
		'GET /v1/projects/acme': PROJECTS[0],
		// Wrapped in the seed's explicit {status, body} form ON PURPOSE: a bare object carrying its own
		// `status` key — which every warehouse record does — is otherwise read as that form, and the
		// record's `"active"` becomes the HTTP status.
		'GET /v1/warehouses/acme-wh': { status: 200, body: WAREHOUSE },
		'GET /v1/table': { tables: TABLES },
	});
});

test('drills project → warehouse → namespace with tier badges along the way', async ({ page }) => {
	// The projects rung: the estate list renders acme with its role + warehouse count.
	await page.goto('/lakehouse/catalog/projects');
	const acmeRow = page.locator('a.row', { hasText: 'acme' });
	await expect(acmeRow).toContainText('admin');
	await expect(acmeRow).toContainText('1 warehouse');
	await acmeRow.click();

	// The project rung: warehouses table links into the warehouse page.
	await expect(page).toHaveURL(/\/catalog\/projects\/acme$/);
	await expect(page.getByRole('heading', { name: 'acme' })).toBeVisible();
	await expect(page.getByText('acme-bucket')).toBeVisible();
	await page.getByRole('link', { name: 'acme-wh' }).click();

	// The warehouse rung: registry facts + namespaces derived by the <project>-<stage> convention,
	// each carrying its tier badge; the foreign project's bare `bronze` zone must NOT appear.
	await expect(page).toHaveURL(/\/catalog\/warehouses\/acme-wh$/);
	await expect(page.getByText('s3://acme-bucket')).toBeVisible();
	const silverRow = page.locator('a.row', { hasText: 'acme-silver' });
	await expect(silverRow).toBeVisible();
	await expect(silverRow.locator('.stage')).toHaveText('silver');
	await expect(page.locator('a.row', { hasText: 'acme-gold' }).locator('.stage')).toHaveText(
		'gold',
	);
	await expect(page.locator('a.row', { hasText: 'bronze$events' })).toHaveCount(0);
	await silverRow.click();

	// The namespace rung (existing page): its member table listed.
	await expect(page).toHaveURL(/\/catalog\/namespaces\/acme-silver$/);
	await expect(page.getByRole('link', { name: 'acme-silver$features' })).toBeVisible();
});

test('a member without the estate privilege still gets their membership gallery', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) =>
		json(route, {
			...ME_ALICE,
			sub: 'user:bob',
			name: 'Bob',
			estate_admin: false,
			projects: [{ project: 'acme', role: 'member' }],
		}),
	);
	await seed(page, { 'GET /v1/projects': { status: 403, body: { detail: 'forbidden' } } });
	await page.goto('/lakehouse/catalog/projects');
	await expect(page.getByText('your memberships')).toBeVisible();
	await expect(page.locator('a.row', { hasText: 'acme' })).toContainText('member');
});
