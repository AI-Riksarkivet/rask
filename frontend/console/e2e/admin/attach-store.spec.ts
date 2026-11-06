import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';

// The stores-by-tier page and its attach form. No door on this stack serves a stores registry
// (`#lib/storage/remote/storage.api.ts`), so the page and the form both say so, and neither sends a
// request: a console calling a door the catalog does not serve would meet a 404 live that a mock hid.

const REASON = 'the catalog serves no stores-by-tier registry';

test.beforeEach(async ({ context, page }, testInfo) => {
	await signIn(context, { token: `${TOKEN.admin}:${testInfo.testId}` });
	await mockMe(page);
});

test('the tier page names the missing registry, and the attach form names it too', async ({
	page,
}) => {
	const sent: string[] = [];
	page.on('request', (req) => {
		const path = new URL(req.url()).pathname;
		if (path.startsWith('/api/catalog/') && !path.endsWith('/management/v1/me')) sent.push(path);
	});
	await page.goto('/catalog/storage');

	await expect(page.getByText(/The stores registry is unavailable \(HTTP 501\)/)).toContainText(
		REASON,
	);

	await page.getByRole('button', { name: 'Attach store' }).click();
	await page.getByLabel('name', { exact: true }).fill('x');
	await page.getByLabel('bucket', { exact: true }).fill('x');
	await page.getByRole('button', { name: 'Attach', exact: true }).click();

	// The refusal's own detail, and the form stays OPEN with the draft intact: a refused write must
	// not read as a completed one.
	await expect(page.getByText(REASON, { exact: false }).last()).toBeVisible();
	await expect(page.getByLabel('name', { exact: true })).toHaveValue('x');
	expect(sent).toEqual([]);
});
