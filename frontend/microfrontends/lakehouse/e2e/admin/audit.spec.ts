import { test, expect, type Route } from '@playwright/test';
import { mockMe, signIn } from './session';

// Hermetic /audit coverage (#77): the viewer reads the #41 compliance trail via the /api/audit BFF. Mock it;
// assert the events render, the outcome filter re-queries, and the nav exposes the route.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const EVENTS = [
	{
		timestamp: '2026-07-20T10:00:00Z',
		action: 'can_drop',
		outcome: 'DENY',
		subject: 'user:bob',
		resource: 'table:db1$t',
	},
	{
		timestamp: '2026-07-20T09:00:00Z',
		action: 'can_read_data',
		outcome: 'ALLOW',
		subject: 'user:alice',
		resource: 'table:db1$t',
	},
];

let lastQuery = '';

test.beforeEach(async ({ context, page }) => {
	await signIn(context); // auth-ON server: the login-first gate redirects signed-out page loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
	lastQuery = '';
	await page.route('**/api/audit**', (route) => {
		const url = new URL(route.request().url());
		lastQuery = url.search;
		const outcome = url.searchParams.get('outcome');
		const events = outcome ? EVENTS.filter((e) => e.outcome === outcome) : EVENTS;
		return json(route, { events });
	});
});

test('a raw GreptimeDB nanosecond timestamp renders as a time, not as an integer', async ({
	page,
}) => {
	// The real BFF returns GreptimeDB's `timestamp` column verbatim: a NANOSECOND epoch integer.
	// `new Date(...)` cannot parse it, so the viewer used to print the integer at the operator.
	const nanos = `${Date.now() - 5 * 60_000}000000`;
	await page.route('**/api/audit**', (route) =>
		json(route, {
			events: [
				{
					timestamp: nanos,
					action: 'can_read_data',
					outcome: 'ALLOW',
					subject: 'user:alice',
					resource: 'table:db1$t',
				},
			],
		}),
	);
	await page.goto('/lakehouse/admin/audit');
	const cell = page.locator('tbody tr').first().locator('td').first();
	await expect(cell).not.toContainText(nanos);
	await expect(cell).toContainText('5m ago');
	// …and the exact stamp is one hover away, as YYYY-MM-DD HH:mm:ss.
	await expect(cell.locator('[title]')).toHaveAttribute(
		'title',
		/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
	);
});

test('renders the audit trail rows', async ({ page }) => {
	await page.goto('/lakehouse/admin/audit');
	await expect(page.getByRole('heading', { name: 'Audit log' })).toBeVisible();
	const table = page.locator('table');
	await expect(table).toContainText('can_drop');
	await expect(table).toContainText('user:bob');
	await expect(table).toContainText('can_read_data');
});

test('the outcome filter re-queries the BFF', async ({ page }) => {
	await page.goto('/lakehouse/admin/audit');
	await expect(page.locator('table')).toContainText('can_read_data');
	// the outcome picker is the @rask/ui Select (bits-ui)
	await page.getByLabel('Outcome filter').click();
	await page.getByRole('option', { name: 'DENY', exact: true }).click();
	await expect.poll(() => lastQuery).toContain('outcome=DENY');
	await expect(page.locator('table')).not.toContainText('can_read_data'); // filtered out
	await expect(page.locator('table')).toContainText('can_drop');
});

test('a row click opens the drawer with the full record and linked context', async ({ page }) => {
	await page.goto('/lakehouse/admin/audit');
	await page.locator('tbody tr', { hasText: 'can_drop' }).click();
	// The drawer carries the full record…
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('user:bob');
	await expect(drawer).toContainText('table:db1$t');
	await expect(drawer).toContainText('DENY');
	// …a cross-zone jump link to the resource page (hard nav)…
	const jump = drawer.getByRole('link', { name: /Open resource/ });
	await expect(jump).toHaveAttribute('href', '/lakehouse/data/tables/db1%24t');
	await expect(jump).toHaveAttribute('data-sveltekit-reload', '');
	// …and the "related events" pivot: filter to this subject re-queries the BFF.
	await drawer.getByRole('button', { name: 'Events by this subject' }).click();
	await expect.poll(() => lastQuery).toContain('subject=user%3Abob');
	await expect(page.getByLabel('Subject filter')).toHaveValue('user:bob');
});

test('a ?resource= deep link lands pre-filtered (the drawers link into this)', async ({ page }) => {
	await page.goto('/lakehouse/admin/audit?resource=table%3Adb1%24t');
	await expect.poll(() => lastQuery).toContain('resource=table%3Adb1%24t');
	await expect(page.getByLabel('Resource filter')).toHaveValue('table:db1$t');
});

test('the shared sidebar marks the Audit leaf active', async ({ page }) => {
	await page.goto('/lakehouse/admin/audit');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Audit' })).toBeVisible();
});
