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
	await page.route('**/api/catalog/management/v1/me', (route) => json(route, ME_ALICE));
	await seed(page, {
		'GET /management/v1/projects': PROJECTS,
		// No `GET /management/v1/projects/acme` seed: the only page that read it was `/catalog/projects/<p>`,
		// which is deleted (see the drill test below).
		// Wrapped in the seed's explicit {status, body} form ON PURPOSE: a bare object carrying its own
		// `status` key — which every warehouse record does — is otherwise read as that form, and the
		// record's `"active"` becomes the HTTP status.
		'GET /management/v1/warehouses/acme-wh': { status: 200, body: WAREHOUSE },
		// The namespaces the REGISTRY binds to this warehouse — the list the page renders (#66).
		'GET /management/v1/warehouses/acme-wh/namespaces': {
			namespaces: ['acme-silver', 'acme-gold'],
		},
		'GET /v1/table': { tables: TABLES },
	});
});

test('drills warehouse → namespace with tier badges along the way', async ({ page }) => {
	// STARTS AT THE WAREHOUSE: a project is the TOP of the hierarchy (project › warehouse › namespace ›
	// table) and its page is `/projects/<p>`; the up-link back to it is pinned below and in
	// warehouses-drawers.spec.ts.
	await page.goto('/catalog/warehouses/acme-wh');

	// The warehouse rung: registry facts + the namespaces the registry binds to it, each carrying its
	// tier badge; a namespace bound elsewhere (`bronze`) must NOT appear.
	await expect(page).toHaveURL(/\/catalog\/warehouses\/acme-wh$/);
	await expect(page.getByText('s3://acme-bucket')).toBeVisible();
	// The way back UP: the project's own page.
	const up = page.getByRole('link', { name: 'acme', exact: true });
	await expect(up).toHaveAttribute('href', '/projects/acme');
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

// DELETED WITH ITS SURFACE, and the gap is named rather than quietly closed: this file used to end
// with 'a member without the estate privilege still gets their membership gallery', which drove the
// projects LIST page — a 403 from /management/v1/projects had to degrade to the caller's own memberships from
// /management/v1/me rather than 500. That page is gone by the ruling above, and the estate's project gallery is
// the console's landing (`home/src/routes/+page.svelte`, which already branches on
// `data.estateAdmin` for exactly this reason).
//
// The BEHAVIOUR still matters and is currently UNTESTED: home's e2e suite runs auth-off with no
// catalog stand-in, so there is nowhere in it to seed a 403 today. Covering it needs home to gain the
// mock-upstream pattern the lakehouse and media suites use. Named here so the deletion reads as a
// move with a debt, not as coverage that silently evaporated.
