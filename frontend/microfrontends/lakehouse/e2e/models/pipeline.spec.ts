import { test, expect, type Page, type Route } from '@playwright/test';

// Hermetic /pipeline coverage (#64): the human trigger door fires the cascade (/medallion/produce) and a
// training run (/medallion/train). Mock the two BFF calls; assert the success banners carry the run token
// and that a 403 renders the project-admin denial (not a silent no-op). This is a pure ACTION page (no
// initial data fetch), so hydration has to be waited for before clicking — otherwise the first click can
// land on a not-yet-hydrated button (no onclick attached).
//
// That wait used to be `networkidle`, and it can never fire again: the shell holds a LIVE query open for
// the notification bell, so this app has no idle network by design. It was always the wrong instrument
// (Playwright discourages it), and the first replacement — `toBeEnabled()` — was wrong in a more
// interesting way: the button is enabled in the SERVER-rendered HTML, so it passes before any listener is
// attached. The click landed on inert markup and the banner never came. The honest condition is "clicking
// this actually does something", so that is what `fire()` asserts: click, and if nothing happened yet,
// click again until it does.
const fire = (page: Page, name: string, banner: string, text: RegExp | string) =>
	expect(async () => {
		await page.getByRole('button', { name }).click();
		await expect(page.locator(banner)).toContainText(text, { timeout: 1000 });
	}).toPass({ timeout: 20_000 });

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('Run cascade fires produce and shows the run token', async ({ page }) => {
	let method = '';
	await page.route('**/medallion/produce', (route) => {
		method = route.request().method();
		return json(route, { status: 'published', token: 'run-abc' }, 202);
	});
	await page.goto('/lakehouse/models/pipeline');
	await expect(page.getByRole('heading', { name: 'Pipeline' })).toBeVisible();
	await fire(page, 'Run cascade', '.banner.ok', 'run-abc');
	expect(method).toBe('POST');
});

test('Request training posts the model + parsed feature datasets', async ({ page }) => {
	let seenBody: { model?: string; features?: { dataset: string }[] } = {};
	await page.route('**/medallion/train', async (route) => {
		seenBody = JSON.parse(route.request().postData() ?? '{}');
		return json(route, { status: 'published', token: 'train-1', model: 'churn' }, 202);
	});
	await page.goto('/lakehouse/models/pipeline');
	// Filling the inputs is itself the hydration proof here: `Request training` stays disabled until the
	// bound `model` state has a value, so it only becomes clickable once the component is live.
	await page.getByLabel('Model name').fill('churn');
	await page.getByLabel('Feature datasets').fill('silver$a, silver$b');
	await expect(page.getByRole('button', { name: 'Request training' })).toBeEnabled();
	await page.getByRole('button', { name: 'Request training' }).click();
	await expect(page.locator('.banner.ok')).toContainText('train-1');
	expect(seenBody.model).toBe('churn');
	expect(seenBody.features).toEqual([{ dataset: 'silver$a' }, { dataset: 'silver$b' }]);
});

test('a 403 renders the project-admin denial banner', async ({ page }) => {
	await page.route('**/medallion/produce', (route) =>
		json(route, { detail: 'produce needs project admin' }, 403),
	);
	await page.goto('/lakehouse/models/pipeline');
	await fire(page, 'Run cascade', '.banner.fail', 'project-admin rung');
});

test('the shared sidebar marks the Pipeline leaf active', async ({ page }) => {
	await page.goto('/lakehouse/models/pipeline');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Pipeline' })).toBeVisible();
});
