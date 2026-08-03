import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_OBS } from '../ports';

// Hermetic /audit coverage (#77). The viewer reads through the remote function now
// (`audit.remote.ts` over `$lib/server/audit-core.ts`), so the mock serves GreptimeDB's RAW /v1/sql
// response per bearer — which upgrades these tests: the rows are seeded in the NESTED
// `log_attributes` JSON-string shape, so the flattening that the 2026-07-21 "every field renders —"
// bug lived in is exercised for real, and the post-filters run in the actual server code instead of
// being simulated by the page.route handler. Filter behaviour is asserted by its RENDERED outcome
// (the wire-query capture died with the browser-side fetch; the render IS the contract).

const SQL_KEY = 'POST /v1/sql?db=public';

/** GreptimeDB's raw /v1/sql shape for the OTLP logs table: `timestamp` column + the attributes
 *  NESTED as a `log_attributes` JSON string (exactly what the real table serves — the flat-column
 *  fixture of the page.route era was the shape that hid the flattening bug). */
const sqlResponse = (
	rows: {
		timestamp: string;
		action: string;
		outcome: string;
		subject: string;
		resource: string;
	}[],
) => ({
	output: [
		{
			records: {
				schema: { column_schemas: [{ name: 'timestamp' }, { name: 'log_attributes' }] },
				rows: rows.map((r) => [
					r.timestamp,
					JSON.stringify({
						'audit.action': r.action,
						'audit.outcome': r.outcome,
						'audit.subject': r.subject,
						'audit.resource': r.resource,
					}),
				]),
			},
		},
	],
});

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

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page); // estate-admin identity: the governance layout door opens
	await page.request.post(`${MOCK_OBS}/__mock/seed`, {
		data: { bearer: token, routes: { [SQL_KEY]: sqlResponse(EVENTS) } },
	});
});

const reseed = (page: Page, rows: typeof EVENTS) =>
	page.request.post(`${MOCK_OBS}/__mock/seed`, {
		data: { bearer: token, routes: { [SQL_KEY]: sqlResponse(rows) } },
	});

test('a raw GreptimeDB nanosecond timestamp renders as a time, not as an integer', async ({
	page,
}) => {
	// The real table returns the `timestamp` column verbatim: a NANOSECOND epoch integer.
	// `new Date(...)` cannot parse it, so the viewer used to print the integer at the operator.
	const nanos = `${Date.now() - 5 * 60_000}000000`;
	await reseed(page, [{ ...EVENTS[1], timestamp: nanos }]);
	await page.goto('/lakehouse/governance/audit');
	const cell = page.locator('tbody tr').first().locator('td').first();
	await expect(cell).not.toContainText(nanos);
	await expect(cell).toContainText('5m ago');
	// …and the exact stamp is one hover away, as YYYY-MM-DD HH:mm:ss.
	await expect(cell.locator('[title]')).toHaveAttribute(
		'title',
		/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
	);
});

test('renders the audit trail rows (through the real log_attributes flattening)', async ({
	page,
}) => {
	await page.goto('/lakehouse/governance/audit');
	await expect(page.getByRole('heading', { name: 'Audit log' })).toBeVisible();
	const table = page.locator('table');
	await expect(table).toContainText('can_drop');
	await expect(table).toContainText('user:bob');
	await expect(table).toContainText('can_read_data');
});

test('the outcome filter re-queries and narrows the trail', async ({ page }) => {
	await page.goto('/lakehouse/governance/audit');
	await expect(page.locator('table')).toContainText('can_read_data');
	// the outcome picker is the @rask/ui Select (bits-ui). The filter runs SERVER-side now (the
	// core's post-filter over the seeded rows) — the narrowed render proves the round trip.
	await page.getByLabel('Outcome filter').click();
	await page.getByRole('option', { name: 'DENY', exact: true }).click();
	await expect(page.locator('table')).not.toContainText('can_read_data'); // filtered out
	await expect(page.locator('table')).toContainText('can_drop');
});

test('a row click opens the drawer with the full record and linked context', async ({ page }) => {
	await page.goto('/lakehouse/governance/audit');
	await page.locator('tbody tr', { hasText: 'can_drop' }).click();
	// The drawer carries the full record…
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('user:bob');
	await expect(drawer).toContainText('table:db1$t');
	await expect(drawer).toContainText('DENY');
	// …a cross-zone jump link to the resource page (hard nav)…
	const jump = drawer.getByRole('link', { name: /Open resource/ });
	await expect(jump).toHaveAttribute('href', '/lakehouse/catalog/tables/db1%24t');
	await expect(jump).toHaveAttribute('data-sveltekit-reload', '');
	// …and the "related events" pivot: filtering to this subject narrows the trail to bob's rows.
	await drawer.getByRole('button', { name: 'Events by this subject' }).click();
	await expect(page.getByLabel('Subject filter')).toHaveValue('user:bob');
	await expect(page.locator('table')).not.toContainText('user:alice');
});

test('a ?resource= deep link lands pre-filtered (the drawers link into this)', async ({ page }) => {
	await page.goto('/lakehouse/governance/audit?resource=table%3Adb1%24t');
	await expect(page.getByLabel('Resource filter')).toHaveValue('table:db1$t');
	await expect(page.locator('table')).toContainText('can_drop'); // the filtered read landed
});

test('the shared sidebar marks the Audit leaf active', async ({ page }) => {
	await page.goto('/lakehouse/governance/audit');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Audit' })).toBeVisible();
});
