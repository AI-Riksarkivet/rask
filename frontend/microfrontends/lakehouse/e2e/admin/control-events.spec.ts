import { expect, test } from '@playwright/test';
import { mockMe, signIn } from './session';
import { MOCK_CATALOG } from '../ports';

// The live control-plane activity feed (query.live). The generator polls the mock catalog (mock-catalog.ts,
// the second webServer) server-side, so we drive it by POSTing to the mock's __mock/* control endpoints —
// a governance mutation elsewhere == an event appearing on the feed. Proves the UI wiring end to end; the
// real mutation → bus → buffer → /v1/events pipeline is proven separately by scripts/verify_control_events.sh.

// Serial: all three tests share the ONE mock-catalog instance (module-level events + mode), so they must
// not run in parallel — else the 403 test's `mode:forbidden` leaks into the OK tests (fullyParallel is on).
test.describe.configure({ mode: 'serial' });

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
	await page.goto('/lakehouse/admin/events');
	await expect(page.getByText('warehouse deactivated')).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText('warehouse:acme')).toBeVisible();
});

test('live-updates when a governance mutation happens elsewhere', async ({ page }) => {
	await page.goto('/lakehouse/admin/events');
	await expect(page.getByText('No recent changes.')).toBeVisible({ timeout: 15_000 });
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
	await page.goto('/lakehouse/admin/events');
	await expect(page.getByText(/Feed unavailable/)).toBeVisible({ timeout: 15_000 });
});
