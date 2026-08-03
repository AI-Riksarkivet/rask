import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// The tenants panel (goal cond 6, rebuilt on the shared DataTable — goal cond 4): one row per
// warehouse with its owning project + effective admins, rendered from the catalog's first-class
// projects API. That read runs on the zone SERVER now (`$lib/admin/remote/tenants.remote.ts`), so
// `page.route` cannot see it — the exact response the old mock served is seeded on the mock catalog
// instead, per BEARER, and everything the panel does with it is asserted unchanged.

const FIXTURE = [
	{
		project: 'acme',
		warehouses: [
			{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' },
			{ id: 'acme-cold', bucket: 'acme-archive', status: 'deactivated' },
		],
		admins: ['alice', 'bob'],
	},
	{
		project: 'beta',
		warehouses: [{ id: 'beta-wh', bucket: 'beta-bucket', status: 'active' }],
		admins: [],
	},
];

let token: string;

/** Seed this test's `/v1/projects` answer — a payload, or a `{status, body}` verdict. */
async function seedProjects(page: import('@playwright/test').Page, answer: unknown): Promise<void> {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: { bearer: token, routes: { 'GET /v1/projects': answer } },
	});
}

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token }); // auth-ON server: the login-first gate redirects signed-out loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
});

test('renders one sortable row per warehouse with project, status, and admins', async ({
	page,
}) => {
	await seedProjects(page, FIXTURE);
	await page.goto('/lakehouse/admin/tenants');
	// acme spans two warehouse rows; the deactivated one carries the warn-toned status.
	const cold = page.locator('tr', { hasText: 'acme-cold' });
	await expect(cold).toContainText('deactivated');
	await expect(cold).toContainText('acme-archive');
	await expect(cold.locator('.chip')).toHaveCount(2); // alice + bob
	// FGA-degraded admins render honestly, not as an error.
	await expect(page.locator('tr', { hasText: 'beta-wh' })).toContainText('none listed');
	// the text search narrows the rows
	await page.getByPlaceholder('Search tenants…').fill('beta');
	await expect(page.locator('tr', { hasText: 'acme-wh' })).toHaveCount(0);
	await expect(page.locator('tr', { hasText: 'beta-wh' })).toBeVisible();
});

test('a row click opens the drawer with the full record and linked context', async ({ page }) => {
	await seedProjects(page, FIXTURE);
	await page.goto('/lakehouse/admin/tenants');
	await page.locator('tbody tr', { hasText: 'acme-cold' }).click();
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('Warehouse acme-cold');
	await expect(drawer).toContainText('acme-archive');
	await expect(drawer).toContainText('deactivated');
	await expect(drawer.locator('.chip')).toHaveCount(2); // alice + bob
	// Linked context: a same-zone filtered audit view + the cross-zone warehouse admin jump.
	await expect(
		drawer.getByRole('link', { name: 'Audit events for this warehouse' }),
	).toHaveAttribute('href', '/lakehouse/governance/audit?resource=acme-cold');
	const jump = drawer.getByRole('link', { name: /Open warehouse admin/ });
	await expect(jump).toHaveAttribute('href', '/lakehouse/catalog/warehouses');
	// The warehouse admin page is in the catalog AREA of this same zone now, so this jump is a soft
	// navigation — forcing a document reload on it would discard the merge's payoff.
	await expect(jump).not.toHaveAttribute('data-sveltekit-reload', '');
});

test('a non-estate-admin sees the forbidden state', async ({ page }) => {
	// The catalog's own verdict, passed through by the remote function exactly as the BFF route did.
	await seedProjects(page, { status: 403, body: { detail: 'forbidden' } });
	await page.goto('/lakehouse/admin/tenants');
	await expect(page.getByText('Tenant enumeration is estate-admin only.')).toBeVisible();
});

test('a payload that drifts from the contract is refused, not rendered', async ({ page }) => {
	// The parse moved to the boundary with the transport; a drift must still read as a drift, never
	// as an outage — the panel renders the two differently.
	await seedProjects(page, [{ project: 'acme' }]);
	await page.goto('/lakehouse/admin/tenants');
	await expect(page.getByText('The projects payload drifted from the contract')).toBeVisible();
});
