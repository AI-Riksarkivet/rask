import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
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

const attached = async (page: import('@playwright/test').Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/access`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { attachedStores: Body[] }).attachedStores;
};

test('attaching a store lands the draft on the catalog and re-renders from its answer', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/storage/tiers');
	await page.getByRole('button', { name: 'Attach store' }).click();
	await page.getByLabel('name', { exact: true }).fill('scans-2024');
	await page.getByLabel('bucket', { exact: true }).fill('scans-2024');
	await page.getByLabel('description').fill('external scan drop');
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
	// The page re-renders from the catalog's ECHOED registry (never a local optimistic append).
	await expect(page.getByText('scans-2024').first()).toBeVisible();
});

test('a denied attach is NAMED in the form — the exact defect the 405 hid', async ({ page }) => {
	await page.request.post(`${MOCK_CATALOG}/__mock/access/config`, {
		data: { bearer: token, failWrites: true },
	});
	await page.goto('/lakehouse/catalog/storage/tiers');
	await page.getByRole('button', { name: 'Attach store' }).click();
	await page.getByLabel('name', { exact: true }).fill('x');
	await page.getByLabel('bucket', { exact: true }).fill('x');
	await page.getByRole('button', { name: 'Attach', exact: true }).click();

	await expect(page.getByText('forbidden')).toBeVisible();
	expect(await attached(page)).toHaveLength(0);
});
