import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// Held promotions on the catalog's publication doors: the list (`GET /management/v1/promotions`) and the
// decision (`POST /management/v1/table/{id}/promotion/{approve,reject}`, carrying the held version).
// Seeded on the mock catalog per bearer; the decision's wire is asserted through its ledger.

type Body = Record<string, unknown>;

const TABLE = 'acme-silver$features';
const LIST = 'GET /management/v1/promotions?limit=100';

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const hold = {
	table: TABLE,
	uuid: '0d6f3c1e-5b1a-4c3e-9f6d-2a8b1c0e9f3a',
	version: 7,
	published_version: 5,
	reasons: ['row_count_delta'],
	opened_at: '2026-11-01T10:00:00Z',
	expires_at: '2026-11-04T10:00:00Z',
	state: 'open',
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
});

test('the list names each held table, its version and why it is held', async ({ page }) => {
	await seed(page, { [LIST]: { holds: [hold], page_token: null } });

	await page.goto('/catalog/promotions');

	const row = page.getByTestId('promotions').locator('tbody tr', { hasText: TABLE });
	await expect(row).toContainText('v7');
	await expect(row).toContainText('Row count moved outside the review band');
	await expect(row.getByRole('link', { name: TABLE })).toHaveAttribute(
		'href',
		`/catalog/promotions/${encodeURIComponent(TABLE)}`,
	);
});

test('a held table renders its version, what published names, and the reason', async ({ page }) => {
	await seed(page, { [LIST]: { holds: [hold], page_token: null } });

	await page.goto(`/catalog/promotions/${encodeURIComponent(TABLE)}`);

	await expect(page.getByText('v7')).toBeVisible();
	await expect(page.getByText('Row count moved outside the review band')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Approve' })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Reject' })).toBeVisible();
});

test('approving posts the held version to the approve door and reports where published stands', async ({
	page,
}) => {
	await seed(page, {
		[LIST]: { holds: [hold], page_token: null },
		[`POST /management/v1/table/${encodeURIComponent(TABLE)}/promotion/approve`]: {
			hold: { ...hold, state: 'approved' },
			published_version: 7,
		},
	});

	await page.goto(`/catalog/promotions/${encodeURIComponent(TABLE)}`);
	await page.getByRole('button', { name: 'Approve' }).click();

	await expect(page.getByText('Approved — published now names v7.')).toBeVisible();
	await expect
		.poll(async () =>
			(await calls(page)).find((c) => String(c.path).endsWith('/promotion/approve')),
		)
		.toMatchObject({ method: 'POST', body: { version: 7 } });
});

test('a table with no open hold says so instead of spinning', async ({ page }) => {
	await seed(page, { [LIST]: { holds: [], page_token: null } });

	await page.goto(`/catalog/promotions/${encodeURIComponent(TABLE)}`);

	await expect(page.getByText(/No open hold on/)).toBeVisible();
	await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0);
});

test('a refusal names the relation, because that is what makes it actionable', async ({ page }) => {
	await seed(page, {
		[LIST]: { holds: [hold], page_token: null },
		[`POST /management/v1/table/${encodeURIComponent(TABLE)}/promotion/approve`]: {
			status: 403,
			body: { detail: 'forbidden' },
		},
	});

	await page.goto(`/catalog/promotions/${encodeURIComponent(TABLE)}`);
	await page.getByRole('button', { name: 'Approve' }).click();

	await expect(page.getByText(/can_promote/)).toBeVisible();
});
