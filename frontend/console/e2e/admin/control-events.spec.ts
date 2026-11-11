import { expect, test } from '@playwright/test';
import { mockMe, signIn } from './session';
import { MOCK_CATALOG } from './ports';

// The live control-plane activity feed (`query.live`). The generator polls the mock catalog
// (mock-catalog.ts) from the browser, and the spec drives it by POSTing to the mock's __mock/* endpoints —
// a governance mutation elsewhere == an event appearing on the feed. Proves the UI wiring end to end; the
// real mutation → bus → buffer → /management/v1/events pipeline is proven separately by scripts/verify_control_events.sh.

// Serial: all three tests share the ONE mock-catalog instance (module-level events + mode), so they must
// not run in parallel — else the 403 test's `mode:forbidden` leaks into the OK tests (fullyParallel is on).
test.describe.configure({ mode: 'serial' });
test.use({ timezoneId: 'UTC' });

const MOCK = MOCK_CATALOG;
const control = (path: string, body?: unknown) =>
	fetch(`${MOCK}/__mock/${path}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: body ? JSON.stringify(body) : undefined,
	});

test.beforeEach(async ({ context, page }) => {
	await signIn(context); // auth-ON server: the login-first gate redirects signed-out page loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
	await control('reset');
});

test('renders recent control-plane events on load', async ({ page }) => {
	await control('add', {
		action: 'warehouse_deactivated',
		object_type: 'warehouse',
		object_id: 'warehouse:acme',
		actor: 'user:alice',
	});
	// A grant names its object by name, with the rung and the subject it went to.
	await control('add', {
		action: 'grant_added',
		object_type: 'grant',
		object_id: 'table:8078bcbc-3d91-4c5e-9d0a-7f1e2b3c4d5e',
		object_name: 'db1$orders',
		actor: 'user:alice',
		extra: { relation: 'reader', subject: 'user:dex~1b6e2f5a-7c4d-4e9b-9f1a-2d3c4b5a6e7f' },
	});
	await page.goto('/admin/events');
	await expect(page.getByText('warehouse deactivated')).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText('warehouse:acme')).toBeVisible();
	const grant = page.locator('li', { hasText: 'grant added' });
	await expect(grant).toContainText('db1$orders');
	await expect(grant).not.toContainText('8078bcbc');
	await expect(grant).toContainText('reader to user:dex~1b6e2f5a-7c4d-4e9b-9f1a-2d3c4b5a6e7f');
	// The full local date and time, with the zone it is read in (this spec's browser runs in UTC).
	await expect(grant.locator('time')).toHaveText('2026-07-23 10:30:00 UTC');
	// A hard load of this page renders the rail's groups open WITH their rows, as every other page does.
	const rail = page.locator('[data-slot="sidebar"]');
	for (const row of ['Tables', 'Events', 'Workbench']) {
		await expect(rail.getByRole('link', { name: row, exact: true })).toBeVisible();
	}
});

test('live-updates when a governance mutation happens elsewhere', async ({ page }) => {
	await page.goto('/admin/events');
	// The feed is the catalog's memory since it started, and an empty one says so.
	await expect(page.getByText('No changes since this catalog started.')).toBeVisible({
		timeout: 15_000,
	});
	// simulate a grant in a "second context": the query.live generator's next poll picks it up and yields.
	await control('add', {
		action: 'grant_added',
		object_type: 'grant',
		object_id: 'table:db1$t',
		actor: 'user:bob',
	});
	await expect(page.getByText('grant added')).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText('table:db1$t')).toBeVisible();
});

test('a non-admin (403) sees a terminal error, not a hammering reconnect', async ({ page }) => {
	await control('mode', { mode: 'forbidden' });
	await page.goto('/admin/events');
	await expect(page.getByText(/Feed unavailable/)).toBeVisible({ timeout: 15_000 });
});
