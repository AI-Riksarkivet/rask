import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG, MOCK_OBS } from '../ports';

// Hermetic /streams coverage. The panel's read is a remote function now (`jetstream.remote.ts`), so
// the RAW `/jsz` payload is seeded on the mock-observability server per bearer and the TRIM runs for
// real on the zone server — which upgrades these tests: they exercise the account flattening, the
// boundness split and the expected-vs-bound diff instead of asserting on a pre-trimmed fixture. The
// dead-subscription expectation list is server env (playwright.config.ts pins
// MEDALLION:raw-to-bronze,TRAINING:lance-ray), so the BASE fixture carries BOUND consumers for both —
// no banner — and the banner tests seed topologies that omit or unbind them. Every behavioural
// assertion of the page.route era is kept.

type RawConsumer = {
	name: string;
	config?: { deliver_group?: string; durable_name?: string };
	push_bound?: boolean;
	num_waiting: number;
	num_pending: number;
	num_ack_pending: number;
	num_redelivered: number;
	delivered?: { last_active?: string };
};

type RawStream = {
	name: string;
	config: {
		subjects: string[];
		retention: string;
		storage: string;
		max_age: number;
		max_msgs: number;
		max_bytes: number;
		num_replicas: number;
	};
	state: {
		messages: number;
		bytes: number;
		first_seq: number;
		last_seq: number;
		consumer_count: number;
	};
	consumer_detail: RawConsumer[];
};

const stream = (
	name: string,
	subjects: string[],
	messages: number,
	consumers: RawConsumer[],
	over: Partial<RawStream['config']> = {},
): RawStream => ({
	name,
	config: {
		subjects,
		retention: 'limits',
		storage: 'file',
		max_age: 0,
		max_msgs: -1,
		max_bytes: -1,
		num_replicas: 1,
		...over,
	},
	state: {
		messages,
		bytes: messages * 400,
		first_seq: 1,
		last_seq: messages,
		consumer_count: consumers.length,
	},
	consumer_detail: consumers,
});

const FRESH = '2026-07-23T16:19:33.986839443Z'; // ~6 min before `now` — never stale

/** A live bound push consumer for `group` — satisfies an expectation. */
const boundConsumer = (
	name: string,
	group: string,
	over: Partial<RawConsumer> = {},
): RawConsumer => ({
	name,
	config: { deliver_group: group, durable_name: name },
	push_bound: true,
	num_waiting: 0,
	num_pending: 0,
	num_ack_pending: 0,
	num_redelivered: 0,
	delivered: { last_active: FRESH },
	...over,
});

/** The two streams the server's expectation list demands, both satisfied — present in every fixture
 *  that must NOT show the banner. */
const EXPECTED_SATISFIED: RawStream[] = [
	stream('MEDALLION', ['medallion.>'], 1, [boundConsumer('raw-to-bronze-d', 'raw-to-bronze')]),
	stream('TRAINING', ['training.>'], 1, [boundConsumer('lance-ray-t', 'lance-ray')]),
];

/** The base topology: the REAL live /jsz sample's DLQ (ephemeral with backlog + redeliveries) and
 *  LINEAGE (durable push), plus the two expectation-satisfying streams. */
const rawBase = (): {
	now: string;
	streams: number;
	consumers: number;
	messages: number;
	bytes: number;
	account_details: { name: string; stream_detail: RawStream[] }[];
} => ({
	now: '2026-07-23T16:25:29.893843405Z',
	streams: 4,
	consumers: 5,
	messages: 141,
	bytes: 365657,
	account_details: [
		{
			name: '$G',
			stream_detail: [
				stream('DLQ', ['dlq.lineage.>'], 5, [
					{
						name: 'fR9hEVt8',
						num_waiting: 1,
						num_pending: 4,
						num_ack_pending: 1,
						num_redelivered: 2,
						delivered: { last_active: FRESH },
					},
				]),
				stream(
					'LINEAGE',
					['lineage.events.>'],
					136,
					[boundConsumer('lance-ray-durable', 'lance-ray')],
					{ max_age: 604800000000000 },
				),
				...EXPECTED_SATISFIED,
			],
		},
	],
});

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page); // estate-admin identity: the admin layout door opens
});

const seedJsz = (page: Page, raw: unknown) =>
	page.request.post(`${MOCK_OBS}/__mock/seed`, {
		data: { bearer: token, routes: { 'GET /jsz?streams=true&consumers=true&config=true': raw } },
	});

test('renders stream cards with consumer lag rows', async ({ page }) => {
	await seedJsz(page, rawBase());
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByRole('heading', { name: 'Streams' })).toBeVisible();

	// Stream cards with their config + state.
	const lineage = page.getByLabel('Stream LINEAGE');
	await expect(lineage).toContainText('lineage.events.>');
	await expect(lineage).toContainText('136 msgs');
	await expect(lineage).toContainText('limits');
	// Consumer lag rows: SERVICE first (the deliver-group-derived estate service), then the name.
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

	// Both expected consumers are BOUND in this fixture and nothing is stale.
	await expect(page.locator('.missingbanner')).toHaveCount(0);
	await expect(page.locator('.stalechip')).toHaveCount(0);
});

test('missing expected consumers render the dead-subscription warn banner', async ({ page }) => {
	// The dead-subscription detector, exercised FOR REAL now: the server diffs the env expectation
	// list against this topology, which omits the MEDALLION and TRAINING streams entirely — an ABSENT
	// consumer is invisible in the raw stream cards, so the panel must shout.
	const raw = rawBase();
	raw.account_details[0].stream_detail = raw.account_details[0].stream_detail.filter(
		(s) => s.name === 'DLQ' || s.name === 'LINEAGE',
	);
	await seedJsz(page, raw);
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
	// The false-negative flavor (audit 2026-07-23): a durable consumer OUTLIVES its subscriber, so
	// the raw topology looks populated while nothing is attached. push_bound=false + num_waiting=0
	// on MEDALLION's durable makes the server flag it `unbound: true`; TRAINING stays bound so the
	// count is exactly one.
	const raw = rawBase();
	for (const s of raw.account_details[0].stream_detail) {
		if (s.name !== 'MEDALLION') continue;
		for (const c of s.consumer_detail) {
			c.push_bound = false;
			c.num_waiting = 0;
		}
	}
	await seedJsz(page, raw);
	await page.goto('/lakehouse/admin/streams');
	const banner = page.locator('.missingbanner');
	await expect(banner).toContainText('1 expected consumer MISSING');
	await expect(banner).toContainText('MEDALLION:raw-to-bronze · present but unbound');
});

test('a consumer inactive for over 10 minutes renders dimmed with a stale chip', async ({
	page,
}) => {
	const raw = rawBase();
	// LINEAGE's consumer last delivered ~66 min before the monitor clock — well past the 10 min bar.
	for (const s of raw.account_details[0].stream_detail) {
		if (s.name !== 'LINEAGE') continue;
		for (const c of s.consumer_detail)
			c.delivered = { last_active: '2026-07-23T15:19:33.986839443Z' };
	}
	await seedJsz(page, raw);
	await page.goto('/lakehouse/admin/streams');

	const lineage = page.getByLabel('Stream LINEAGE');
	// The DataTable rows dim per-CELL (.stale spans) with one chip on the last-active cell.
	await expect(lineage.locator('.stalechip')).toHaveCount(1);
	await expect(lineage.locator('.stalechip')).toHaveText('stale');
	// The DLQ consumer (active ~6 min before `now`) stays fresh — staleness is per-consumer.
	await expect(page.getByLabel('Stream DLQ').locator('.stalechip')).toHaveCount(0);
});

test('Refresh re-queries and shows +N delta chips', async ({ page }) => {
	await seedJsz(page, rawBase());
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByLabel('Stream LINEAGE')).toBeVisible();
	// First render: no baseline yet, so no delta chips.
	await expect(page.locator('.delta')).toHaveCount(0);
	// Second poll: +9 total, +9 on LINEAGE — the chips compare against the previous rendered poll.
	const grown = rawBase();
	grown.messages = 150;
	for (const s of grown.account_details[0].stream_detail) {
		if (s.name === 'LINEAGE') s.state.messages = 145;
	}
	await seedJsz(page, grown);
	await page.getByRole('button', { name: 'Refresh' }).click();
	// Totals chip + the LINEAGE stream chip (DLQ is unchanged → no chip).
	await expect(page.locator('.bar .delta')).toHaveText('+9');
	await expect(page.getByLabel('Stream LINEAGE').locator('.delta')).toHaveText('+9');
	await expect(page.getByLabel('Stream DLQ').locator('.delta')).toHaveCount(0);
});

test('a shrinking stream renders a neutral negative delta with an accurate tooltip', async ({
	page,
}) => {
	await seedJsz(page, rawBase());
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByLabel('Stream DLQ')).toBeVisible();
	// Second poll: DLQ drained 5 → 2 (e.g. a replay) — shrinkage, not growth.
	const drained = rawBase();
	drained.messages = 138;
	for (const s of drained.account_details[0].stream_detail) {
		if (s.name === 'DLQ') s.state.messages = 2;
	}
	await seedJsz(page, drained);
	await page.getByRole('button', { name: 'Refresh' }).click();
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
	await seedJsz(page, rawBase());
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

test('an estate-admin denial renders the forbidden state', async ({ page }) => {
	// The gate is the server-side estate-admin probe against the catalog now — flip THIS bearer's
	// /v1/events to 403 on the mock catalog and the remote function answers the denial.
	await page.request.post(`${MOCK_CATALOG}/__mock/mode`, {
		data: { bearer: token, mode: 'forbidden' },
	});
	await seedJsz(page, rawBase());
	await page.goto('/lakehouse/admin/streams');
	await expect(page.getByText('The stream view is estate-admin only')).toBeVisible();
	await expect(page.locator('table')).toHaveCount(0);
});
