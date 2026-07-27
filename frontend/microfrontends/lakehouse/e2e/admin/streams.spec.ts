import { test, expect, type Route } from '@playwright/test';
import { mockMe, signIn } from './session';

// Hermetic /streams coverage: the panel reads the trimmed JetStream overview via the /api/jetstream BFF.
// Mock it; assert the stream cards + consumer lag rows render (service column first, DLQ flagged,
// redelivery highlighted, stale consumers dimmed+chipped), the missing-consumer banner (the
// dead-subscription detector), the "+N since the last change" delta chips, and that a 403 renders the
// forbidden state.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// Trimmed form of the REAL live /jsz sample (kind cluster, 2026-07-23): LINEAGE with its durable push
// consumer, plus the DLQ stream carrying an ephemeral consumer with backlog + redeliveries. `now` is
// within 10 min of every last_active so no consumer renders stale in the base fixture.
const OVERVIEW = {
	now: '2026-07-23T16:25:29.893843405Z',
	totals: { streams: 2, consumers: 3, messages: 141, bytes: 365657 },
	streams: [
		{
			name: 'DLQ',
			subjects: ['dlq.lineage.>'],
			retention: 'limits',
			storage: 'file',
			max_age_ns: 0,
			max_msgs: -1,
			max_bytes: -1,
			num_replicas: 1,
			state: { messages: 5, bytes: 2000, first_seq: 1, last_seq: 5, consumer_count: 1 },
			consumers: [
				{
					name: 'fR9hEVt8',
					service: '(ephemeral)',
					durable: false,
					num_pending: 4,
					num_ack_pending: 1,
					num_redelivered: 2,
					last_active: '2026-07-23T16:19:33.986839443Z',
				},
			],
		},
		{
			name: 'LINEAGE',
			subjects: ['lineage.events.>'],
			retention: 'limits',
			storage: 'file',
			max_age_ns: 604800000000000,
			max_msgs: -1,
			max_bytes: -1,
			num_replicas: 1,
			state: { messages: 136, bytes: 363657, first_seq: 10496, last_seq: 10631, consumer_count: 2 },
			consumers: [
				{
					name: 'lance-ray-durable',
					service: 'lance-ray',
					durable: true,
					deliver_group: 'lance-ray',
					num_pending: 0,
					num_ack_pending: 0,
					num_redelivered: 0,
					last_active: '2026-07-23T16:19:33.986839443Z',
				},
			],
		},
	],
	missing: [],
};

test.beforeEach(async ({ context, page }) => {
	await signIn(context); // auth-ON server: the login-first gate redirects signed-out page loads
	await mockMe(page); // estate-admin identity: the admin layout door opens
});

test('renders stream cards with consumer lag rows', async ({ page }) => {
	await page.route('**/api/jetstream*', (route) => json(route, OVERVIEW));
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByRole('heading', { name: 'Streams' })).toBeVisible();

	// Stream cards with their config + state.
	const lineage = page.getByLabel('Stream LINEAGE');
	await expect(lineage).toContainText('lineage.events.>');
	await expect(lineage).toContainText('136 msgs');
	await expect(lineage).toContainText('limits');
	// Consumer lag rows: SERVICE first (the BFF-derived estate service), then the consumer name.
	await expect(lineage.locator('th').first()).toHaveText('service');
	await expect(lineage.locator('.service')).toHaveText('lance-ray');
	await expect(lineage.locator('table')).toContainText('lance-ray-durable');

	// The DLQ stream is visually flagged, and its ephemeral consumer shows backlog + redeliveries; a
	// group-less ephemeral outside CATALOG_CONTROL gets the "(ephemeral)" service placeholder.
	const dlq = page.getByLabel('Stream DLQ');
	await expect(dlq).toContainText('DLQ');
	await expect(dlq.locator('.badge.dlqbadge')).toBeVisible();
	await expect(dlq.locator('.service')).toHaveText('(ephemeral)');
	await expect(dlq.locator('table')).toContainText('fR9hEVt8 (ephemeral)');
	await expect(dlq.locator('.warn')).toHaveText('2'); // redelivered > 0 highlighted

	// No expected consumer is missing and nothing is stale in the base fixture.
	await expect(page.locator('.missingbanner')).toHaveCount(0);
	await expect(page.locator('.stalechip')).toHaveCount(0);
});

test('missing expected consumers render the dead-subscription warn banner', async ({ page }) => {
	// The dead-subscription detector: the BFF diffed JETSTREAM_EXPECTED_CONSUMERS against the live
	// topology and found two expected groups absent — the panel must shout, because an ABSENT consumer
	// is invisible in the raw stream cards themselves.
	await page.route('**/api/jetstream*', (route) =>
		json(route, {
			...OVERVIEW,
			missing: [
				{ stream: 'MEDALLION', service: 'raw-to-bronze' },
				{ stream: 'TRAINING', service: 'lance-ray' },
			],
		}),
	);
	await page.goto('/lakehouse/admin/streams');
	const banner = page.locator('.missingbanner');
	await expect(banner).toContainText('2 expected consumers MISSING');
	await expect(banner).toContainText('MEDALLION:raw-to-bronze');
	await expect(banner).toContainText('TRAINING:lance-ray');
	// The ordinary stream cards still render alongside the banner.
	await expect(page.getByLabel('Stream LINEAGE')).toBeVisible();
});

test('an existing-but-unbound durable fires the banner as "present but unbound"', async ({
	page,
}) => {
	// The false-negative flavor (audit 2026-07-23): a durable consumer OUTLIVES its subscriber, so the
	// raw topology looks populated while nothing is attached. The BFF (push_bound false / num_waiting 0)
	// flags the expected group `unbound: true`; the panel must still shout, and name the flavor.
	await page.route('**/api/jetstream*', (route) =>
		json(route, {
			...OVERVIEW,
			missing: [{ stream: 'MEDALLION', service: 'raw-to-bronze', unbound: true }],
		}),
	);
	await page.goto('/lakehouse/admin/streams');
	const banner = page.locator('.missingbanner');
	await expect(banner).toContainText('1 expected consumer MISSING');
	await expect(banner).toContainText('MEDALLION:raw-to-bronze · present but unbound');
});

test('a consumer inactive for over 10 minutes renders dimmed with a stale chip', async ({
	page,
}) => {
	const stale = structuredClone(OVERVIEW);
	// LINEAGE's consumer last delivered ~66 min before the monitor clock — well past the 10 min bar.
	for (const s of stale.streams) {
		if (s.name !== 'LINEAGE') continue;
		for (const c of s.consumers) c.last_active = '2026-07-23T15:19:33.986839443Z';
	}
	await page.route('**/api/jetstream*', (route) => json(route, stale));
	await page.goto('/lakehouse/admin/streams');

	const lineage = page.getByLabel('Stream LINEAGE');
	// The DataTable rows dim per-CELL (.stale spans) with one chip on the last-active cell.
	await expect(lineage.locator('.stalechip')).toHaveCount(1);
	await expect(lineage.locator('.stalechip')).toHaveText('stale');
	// The DLQ consumer (active ~6 min before `now`) stays fresh — staleness is per-consumer.
	await expect(page.getByLabel('Stream DLQ').locator('.stalechip')).toHaveCount(0);
});

test('Refresh re-queries the BFF and shows +N delta chips', async ({ page }) => {
	let calls = 0;
	await page.route('**/api/jetstream*', (route) => {
		calls += 1;
		if (calls === 1) return json(route, OVERVIEW);
		// Second poll: +9 total, +9 on LINEAGE — the chips compare against the previous rendered poll.
		const grown = structuredClone(OVERVIEW);
		grown.totals.messages = 150;
		for (const s of grown.streams) if (s.name === 'LINEAGE') s.state.messages = 145;
		return json(route, grown);
	});
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByLabel('Stream LINEAGE')).toBeVisible();
	// First render: no baseline yet, so no delta chips.
	await expect(page.locator('.delta')).toHaveCount(0);
	const before = calls;
	await page.getByRole('button', { name: 'Refresh' }).click();
	await expect.poll(() => calls).toBeGreaterThan(before);
	// Totals chip + the LINEAGE stream chip (DLQ is unchanged → no chip).
	await expect(page.locator('.bar .delta')).toHaveText('+9');
	await expect(page.getByLabel('Stream LINEAGE').locator('.delta')).toHaveText('+9');
	await expect(page.getByLabel('Stream DLQ').locator('.delta')).toHaveCount(0);
});

test('a shrinking stream renders a neutral negative delta with an accurate tooltip', async ({
	page,
}) => {
	let calls = 0;
	await page.route('**/api/jetstream*', (route) => {
		calls += 1;
		if (calls === 1) return json(route, OVERVIEW);
		// Second poll: DLQ drained 5 → 2 (e.g. a replay) — shrinkage, not growth.
		const drained = structuredClone(OVERVIEW);
		drained.totals.messages = 138;
		for (const s of drained.streams) if (s.name === 'DLQ') s.state.messages = 2;
		return json(route, drained);
	});
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByLabel('Stream DLQ')).toBeVisible();
	const before = calls;
	await page.getByRole('button', { name: 'Refresh' }).click();
	await expect.poll(() => calls).toBeGreaterThan(before);
	// Negative chips carry the neutral `neg` styling and say REMOVED, never the growth-green "+N".
	const dlqDelta = page.getByLabel('Stream DLQ').locator('.delta');
	await expect(dlqDelta).toHaveText('-3');
	await expect(dlqDelta).toHaveClass(/neg/);
	await expect(dlqDelta).toHaveAttribute('title', /removed since the last change/);
	const totalsDelta = page.locator('.bar .delta');
	await expect(totalsDelta).toHaveText('-3');
	await expect(totalsDelta).toHaveClass(/neg/);
});

test('a consumer row click opens the drawer with the full record and diagnosis', async ({
	page,
}) => {
	await page.route('**/api/jetstream*', (route) => json(route, OVERVIEW));
	await page.goto('/lakehouse/admin/streams');
	await page.getByLabel('Stream DLQ').locator('tbody tr', { hasText: 'fR9hEVt8' }).click();
	const drawer = page.locator('[data-slot="sheet-content"]');
	await expect(drawer).toContainText('Consumer fR9hEVt8');
	// The full record: stream, service, lag counters, and the wedge diagnosis for redelivered > 0.
	await expect(drawer).toContainText('DLQ');
	await expect(drawer).toContainText('(ephemeral)');
	await expect(drawer.locator('.record')).toContainText('4'); // pending
	await expect(drawer.locator('.record .warn')).toHaveText('2'); // redelivered, warn-toned
	await expect(drawer.locator('.diagnosis')).toContainText('wedge signal');
	// A DLQ-stream consumer links straight to the DLQ ops panel (same zone).
	await expect(drawer.getByRole('link', { name: /DLQ ops panel/ })).toHaveAttribute(
		'href',
		'/lakehouse/admin/dlq',
	);
});

test('a 403 from the BFF renders the forbidden state', async ({ page }) => {
	await page.route('**/api/jetstream*', (route) =>
		json(route, { detail: 'the stream view is estate-admin only' }, 403),
	);
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByText('The stream view is estate-admin only')).toBeVisible();
	await expect(page.locator('table')).toHaveCount(0);
});
