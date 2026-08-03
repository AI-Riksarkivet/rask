import { test, expect } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_LINEAGE } from '../ports';

// Hermetic /dlq coverage (#83): the ops panel reads the transactional-outbox backlog and replays one
// staged event. Both run on the zone SERVER now (`$lib/lineage/remote/lineage.remote.ts`), so
// `page.route` cannot see either — the outbox is seeded on the mock lineage service instead, per
// BEARER, and the replay is asserted through that service's own ledger. Same assertions as before the
// transport moved: the events render, a replay drains the event out of the list, poison is
// unreplayable, and the nav exposes the route.

const EVENTS = [
	{
		run_id: 'run-1',
		event_type: 'COMPLETE',
		job: 'ingest',
		outputs: ['bronze$a'],
		inputs: [],
		parseable: true,
	},
	{ run_id: 'bad-1', event_type: null, job: null, outputs: [], inputs: [], parseable: false },
];

let token: string;

/** Seed THIS test's outbox. The replay endpoint drains from the same list, exactly like the real
 *  relay (re-ingest + drop), so the post-replay re-read shrinks on its own. */
async function seedOutbox(
	page: import('@playwright/test').Page,
	events: unknown[] = EVENTS,
): Promise<void> {
	await page.request.post(`${MOCK_LINEAGE}/__mock/dlq`, { data: { bearer: token, events } });
}

/** What this bearer's zone server actually POSTed to the lineage plane. */
async function calls(page: import('@playwright/test').Page): Promise<{ path: string }[]> {
	const res = await page.request.get(`${MOCK_LINEAGE}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: { path: string }[] }).calls;
}

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token }); // auth-ON server: the login-first gate redirects signed-out loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
	await seedOutbox(page);
});

test('renders the outbox at-risk events with a depth badge', async ({ page }) => {
	await page.goto('/lakehouse/admin/dlq');
	await expect(page.getByRole('heading', { name: 'Lineage DLQ' })).toBeVisible();
	const table = page.locator('table');
	await expect(table).toContainText('run-1');
	await expect(table).toContainText('bronze$a');
	await expect(table).toContainText('poison'); // the unparseable object is surfaced
	await expect(page.locator('.depth')).toContainText('depth 2');
});

test('replay reaches the lineage plane and the drained event leaves the list', async ({ page }) => {
	await page.goto('/lakehouse/admin/dlq');
	await expect(page.locator('table')).toContainText('run-1');
	// exact: the clickable ROW is also role=button and its name contains "run-1" (drawer, cond 8).
	await page.getByRole('button', { name: 'Replay run-1', exact: true }).click();
	await expect(page.locator('.msg')).toContainText('Replayed run-1');
	// the command really landed upstream — asserted at the service, not at the browser boundary
	await expect
		.poll(async () => (await calls(page)).map((c) => c.path))
		.toContain('/admin/dlq/run-1/replay');
	// the reload drops the drained event; only the poison row remains
	await expect(page.locator('table')).not.toContainText('run-1');
	await expect(page.locator('table')).toContainText('poison');
});

test('a poison object is not replayable', async ({ page }) => {
	await page.goto('/lakehouse/admin/dlq');
	const poisonRow = page.locator('tr', { has: page.locator('.poison') });
	await expect(poisonRow).toContainText('unreplayable');
	await expect(poisonRow.getByRole('button', { name: /Replay/ })).toHaveCount(0);
});

test('shows the honest empty state when the outbox is drained', async ({ page }) => {
	await seedOutbox(page, []);
	await page.goto('/lakehouse/admin/dlq');
	await expect(page.getByText('The outbox is empty')).toBeVisible();
});

test('a row click opens the drawer with the staged payload and a replay action', async ({
	page,
}) => {
	await page.goto('/lakehouse/admin/dlq');
	await page.locator('tbody tr', { hasText: 'run-1' }).click();
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('Staged event run-1');
	// The payload block shows the full parsed record.
	await expect(drawer.getByLabel('Staged event payload')).toContainText('"event_type": "COMPLETE"');
	await expect(drawer.getByLabel('Staged event payload')).toContainText('"bronze$a"');
	// The output dataset jump link hard-navigates cross-zone.
	const jump = drawer.getByRole('link', { name: /bronze\$a/ });
	await expect(jump).toHaveAttribute('href', '/lakehouse/catalog/tables/bronze%24a');
	// Replay from the drawer drains the event (the drawer closes; the bar reports).
	await drawer.getByRole('button', { name: 'Replay this event' }).click();
	await expect(page.locator('.msg')).toContainText('Replayed run-1');
	await expect(page.locator('table')).not.toContainText('run-1');
});

test('the poison drawer explains unreplayability and offers no replay', async ({ page }) => {
	await page.goto('/lakehouse/admin/dlq');
	await page.locator('tbody tr', { hasText: 'bad-1' }).click();
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('poison object');
	await expect(drawer.getByRole('button', { name: 'Replay this event' })).toHaveCount(0);
});

test('the shared sidebar marks the DLQ leaf active', async ({ page }) => {
	await page.goto('/lakehouse/admin/dlq');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'DLQ' })).toBeVisible();
});
