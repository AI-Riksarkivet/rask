import { expect, test } from '@playwright/test';
import { mockMe, signIn } from './session';

// The tenants panel (goal cond 6, rebuilt on the shared DataTable — goal cond 4): one row per
// warehouse with its owning project + effective admins, rendered from the catalog's first-class
// projects API through the bearer-forwarding BFF. Hermetic: the BFF route is same-origin, so
// page.route mocks it.

test.beforeEach(async ({ context, page }) => {
	await signIn(context); // auth-ON server: the login-first gate redirects signed-out page loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
});

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

test('renders one sortable row per warehouse with project, status, and admins', async ({
	page,
}) => {
	await page.route('**/api/projects*', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FIXTURE) }),
	);
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
	await page.route('**/api/projects*', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FIXTURE) }),
	);
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
	).toHaveAttribute('href', '/lakehouse/admin/audit?resource=acme-cold');
	const jump = drawer.getByRole('link', { name: /Open warehouse admin/ });
	await expect(jump).toHaveAttribute('href', '/lakehouse/data/warehouses');
	// The warehouse admin page is in the catalog AREA of this same zone now, so this jump is a soft
	// navigation — forcing a document reload on it would discard the merge's payoff.
	await expect(jump).not.toHaveAttribute('data-sveltekit-reload', '');
});

test('a non-estate-admin sees the forbidden state', async ({ page }) => {
	await page.route('**/api/projects*', (route) =>
		route.fulfill({ status: 403, contentType: 'application/json', body: '{"detail":"forbidden"}' }),
	);
	await page.goto('/lakehouse/admin/tenants');
	await expect(page.getByText('Tenant enumeration is estate-admin only.')).toBeVisible();
});
