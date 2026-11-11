import { expect, test } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';

// The stores-by-tier page. No door on this stack serves a stores registry
// (`#lib/storage/remote/storage.api.ts`), so the page says so, sends no request, and offers no write
// that could not act: a console calling a door the catalog does not serve would meet a 404 live that a
// mock hid.

const REASON = 'the catalog serves no stores-by-tier registry';

test.beforeEach(async ({ context, page }, testInfo) => {
	await signIn(context, { token: `${TOKEN.admin}:${testInfo.testId}` });
	await mockMe(page);
});

test('the tier page names the missing registry and offers no attach', async ({ page }) => {
	const sent: string[] = [];
	page.on('request', (req) => {
		const path = new URL(req.url()).pathname;
		if (path.startsWith('/api/catalog/') && !path.endsWith('/management/v1/me')) sent.push(path);
	});
	await page.goto('/catalog/storage');

	await expect(page.getByText(/The stores registry is unavailable \(HTTP 501\)/)).toContainText(
		REASON,
	);

	await expect(page.getByRole('button', { name: 'Attach store' })).toHaveCount(0);
	expect(sent).toEqual([]);
});
