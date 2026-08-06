import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { STORES } from './store-fixtures';
import { MOCK_CATALOG } from '../ports';

// The attach-store flow, end to end through the remote command — the regression fence for the 405
// this migration fixed: `attachStore` used to POST through the GET-only /capi catch-all, so the
// attach form was dead on arrival and nothing said so. The write runs on the zone SERVER now, so it
// is asserted through the mock catalog's per-bearer ledger (page.route cannot see it).

type Body = Record<string, unknown>;

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
});

const attached = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/access`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { attachedStores: Body[] }).attachedStores;
};

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

/** The catalog derives `/v1/stores/tiers` itself — so does the fixture, from whatever registry the
 *  test says the catalog holds at that moment. */
const tiersOf = (stores: Array<{ role: string }>): Record<string, unknown[]> => {
	const tiers: Record<string, unknown[]> = {};
	for (const s of stores) (tiers[s.role] ??= []).push(s);
	return tiers;
};

/** The tier page's list is rendered by a client `$effect`, never by SSR, so its first row appearing
 *  is proof the route has HYDRATED. Every interaction below must wait for it: this route is the one
 *  admin path `e2e/admin/warmup.setup.ts` does not warm, so it pays a cold Vite transform on the
 *  auth-ON server and `page.goto` (which resolves at `load`) returns well before SvelteKit has
 *  attached the Attach-store button's listener. Clicking into that gap is silently swallowed — the
 *  form never opens and the first `fill` burns the whole 30 s timeout. */
const openAttachForm = async (page: Page): Promise<void> => {
	await expect(page.getByText('s3://rask-wh')).toBeVisible();
	await page.getByRole('button', { name: 'Attach store' }).click();
	await expect(page.getByRole('heading', { name: 'Attach a store' })).toBeVisible();
};

test('attaching a store lands the draft on the catalog and re-renders from its answer', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/stores/tiers': tiersOf(STORES) });
	await page.goto('/lakehouse/catalog/storage/tiers');
	await openAttachForm(page);
	await page.getByLabel('name', { exact: true }).fill('scans-2024');
	await page.getByLabel('bucket', { exact: true }).fill('scans-2024');
	await page.getByLabel('description').fill('external scan drop');

	// The catalog's world AFTER the attach. Seeded once the pre-attach registry is on screen, so the
	// re-read the form triggers lands on the MOVED registry — the mock's built-in `/v1/stores/tiers`
	// is derived from a static fixture and cannot express "and then this arrived".
	const landed = {
		name: 'scans-2024',
		bucket: 'scans-2024',
		role: 'raw',
		description: 'external scan drop',
		read_only: true,
	};
	await seed(page, { 'GET /v1/stores/tiers': tiersOf([...STORES, landed]) });
	await page.getByRole('button', { name: 'Attach', exact: true }).click();

	await expect.poll(async () => (await attached(page)).length).toBe(1);
	// Blank endpoint is normalised to null BEFORE the wire — "this deployment's own S3" must reach
	// the catalog as the absence it means, not as an empty string it would try to dial.
	expect((await attached(page))[0]).toMatchObject({
		name: 'scans-2024',
		bucket: 'scans-2024',
		role: 'raw',
		endpoint: null,
	});
	// The page re-renders from the CATALOG's registry (never a local optimistic append): the new row
	// appears under `raw`, the role the catalog filed it under, with the catalog's own read-only flag
	// — neither of which the browser sent.
	const raw = page.locator('article[data-role="raw"]');
	await expect(raw.locator('li', { hasText: 's3://scans-2024' })).toContainText('read-only');
});

test('a denied attach is NAMED in the form — the exact defect the 405 hid', async ({ page }) => {
	await page.request.post(`${MOCK_CATALOG}/__mock/access/config`, {
		data: { bearer: token, failWrites: true },
	});
	await page.goto('/lakehouse/catalog/storage/tiers');
	await openAttachForm(page);
	await page.getByLabel('name', { exact: true }).fill('x');
	await page.getByLabel('bucket', { exact: true }).fill('x');
	await page.getByRole('button', { name: 'Attach', exact: true }).click();

	// The catalog's OWN detail, verbatim and on its own — exact, so this cannot pass on some other
	// element that merely contains the word.
	await expect(page.getByText('forbidden', { exact: true })).toBeVisible();
	// The form stays OPEN with the draft intact — a denied write must not read as a completed one.
	await expect(page.getByLabel('name', { exact: true })).toHaveValue('x');
	expect(await attached(page)).toHaveLength(0);
});
