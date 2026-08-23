import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_OBS } from '../ports';

// The promotion DECISION surface — the estate's only door for answering a held promotion.
//
// Why this spec exists at all: the medallion parks a durable workflow on
// `wait_for_external_event('promotion_decision')` for up to 72 hours, and until this route landed the
// only way to answer one was curl. A surface that exists but cannot be driven is not a feature, so the
// three states a validator actually meets are pinned here.
//
// SEEDED ON THE GATEWAY, not page.route: both the read and the decision are remote functions, so they
// run on the zone SERVER where the browser cannot intercept them. RASK_GATEWAY_URL points at the
// seed-driven mock (playwright.config.ts, auth-ON project) and the wire is asserted through its ledger.

type Body = Record<string, unknown>;

const INSTANCE = 'promotion-tok-1';

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_OBS}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_OBS}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const held = {
	instance_id: INSTANCE,
	project: 'acme',
	from_dataset: 'acme-bronze$events',
	to_dataset: 'acme-silver$features',
	reasons: ['row_count_delta'],
	approval_hours: 72,
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
});

test('a live review renders its destination and the reason a person is being asked', async ({
	page,
}) => {
	await seed(page, { [`GET /api/promotions/${INSTANCE}`]: held });

	await page.goto(`/lakehouse/catalog/promotions/${INSTANCE}`);

	await expect(page.getByText('acme-silver$features')).toBeVisible();
	// The REASON, rendered for a person rather than as the raw code — the whole point of asking.
	await expect(page.getByText('Row count moved outside the review band')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Approve' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Reject' })).toBeVisible();
});

test('approving posts the decision and reports what it released', async ({ page }) => {
	await seed(page, {
		[`GET /api/promotions/${INSTANCE}`]: held,
		[`POST /api/promotions/${INSTANCE}/decision`]: {
			status: 'accepted',
			instance_id: INSTANCE,
			approved: true,
			dataset: 'acme-silver$features',
		},
	});

	await page.goto(`/lakehouse/catalog/promotions/${INSTANCE}`);
	await page.getByRole('button', { name: 'Approve' }).click();

	await expect(page.getByText(/Approved — the cascade resumed/)).toBeVisible();

	// The WIRE, not just the rendering: the decision must actually reach the door, with approved=true.
	const decision = (await calls(page)).find((c) => String(c.path).endsWith('/decision'));
	expect(decision).toBeTruthy();
	expect((decision?.body as Body | undefined)?.approved).toBe(true);
});

test('an already-decided review says so instead of spinning', async ({ page }) => {
	// 404 is the ORDINARY terminal state here — decided already, or the 72h window closed. A validator
	// following a stale link must be told that plainly rather than shown an empty card.
	await seed(page, {
		[`GET /api/promotions/${INSTANCE}`]: { status: 404, body: { detail: 'Not Found' } },
	});

	await page.goto(`/lakehouse/catalog/promotions/${INSTANCE}`);

	await expect(page.getByText(/No live review under/)).toBeVisible();
	await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0);
});

test('a refusal names the relation, because that is what makes it actionable', async ({ page }) => {
	// The rung is `can_promote` on the destination namespace — above the ordinary publish's
	// `can_update_tag`. A validator who lacks it needs the relation NAMED so they can ask for it.
	await seed(page, {
		[`GET /api/promotions/${INSTANCE}`]: held,
		[`POST /api/promotions/${INSTANCE}/decision`]: { status: 403, body: { detail: 'forbidden' } },
	});

	await page.goto(`/lakehouse/catalog/promotions/${INSTANCE}`);
	await page.getByRole('button', { name: 'Approve' }).click();

	await expect(page.getByText(/can_promote/)).toBeVisible();
});
