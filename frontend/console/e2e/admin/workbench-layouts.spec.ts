import { expect, test, type Page } from '@playwright/test';
import { signIn } from './session';

// The workbench keeps a person's layout and named views in this browser, under a key derived from
// who signed in. Signed-in specs, because the key is the signed-in principal's and a reload signs in
// again (tokens live in memory, so the mock issuer is visited on every page load).

const tab = (page: Page, title: string) => page.getByRole('tab', { name: new RegExp(`^${title}`) });

test.beforeEach(async ({ context }) => {
	await signIn(context);
});

test('a rearranged layout survives a reload and its sign-in, even one right after the change', async ({
	page,
}) => {
	await page.goto('/workbench');
	await expect(tab(page, 'Events')).toBeVisible();
	await page.getByRole('button', { name: 'Close Events' }).click();
	await expect(tab(page, 'Events')).toHaveCount(0);
	// No wait for the debounced save: leaving the page writes what is pending.
	await page.reload();
	await expect(tab(page, 'Tables')).toBeVisible();
	await expect(tab(page, 'Events')).toHaveCount(0);
});

test('a saved named view is applied from the sidebar', async ({ page }) => {
	await page.goto('/workbench');
	await expect(tab(page, 'Events')).toBeVisible();
	await page.getByRole('button', { name: 'New view' }).click();
	await page.getByLabel('View name').fill('Side by side');
	await page.getByLabel('View name').press('Enter');
	await expect(page.getByRole('button', { name: 'Side by side' })).toBeVisible();

	await page.getByRole('button', { name: 'Close Events' }).click();
	await expect(tab(page, 'Events')).toHaveCount(0);
	await page.getByRole('button', { name: 'Side by side' }).click();
	await expect(tab(page, 'Events')).toBeVisible();
	await expect(page.getByRole('tablist')).toHaveCount(2);
});
