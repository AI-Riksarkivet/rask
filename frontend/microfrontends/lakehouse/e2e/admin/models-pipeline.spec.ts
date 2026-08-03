import { test, expect, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_OBS } from '../ports';

// Hermetic /pipeline coverage (#64), moved from e2e/models when the trigger door became remote
// commands (`medallion.remote.ts`): the calls run on the zone SERVER against MEDALLION_API, which
// this project's dev server points at the seed-driven mock-observability server. Every behavioural
// assertion of the original is kept: the success banners carry the run token, the exact train wire
// body reaches the upstream (asserted from the mock's per-bearer call ledger now), a 403 renders the
// project-admin denial, and the sidebar marks the leaf active.
//
// The `fire` helper survives verbatim, and for the same reason: this is a pure ACTION page, the shell
// holds a live query open (no network idle by design), and the SERVER-rendered button is enabled
// before hydration attaches its listener — so "clicking actually does something" is the only honest
// readiness condition.
const fire = (page: Page, name: string, banner: string, text: RegExp | string) =>
	expect(async () => {
		await page.getByRole('button', { name }).click();
		await expect(page.locator(banner)).toContainText(text, { timeout: 1000 });
	}).toPass({ timeout: 20_000 });

let token: string;

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
});

const seed = (page: Page, routes: Record<string, unknown>) =>
	page.request.post(`${MOCK_OBS}/__mock/seed`, { data: { bearer: token, routes } });

const calls = async (page: Page): Promise<Record<string, unknown>[]> => {
	const res = await page.request.get(`${MOCK_OBS}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Record<string, unknown>[] }).calls;
};

test('Run cascade fires produce and shows the run token', async ({ page }) => {
	await seed(page, {
		'POST /produce': { status: 202, body: { status: 'published', token: 'run-abc' } },
	});
	await page.goto('/lakehouse/models/pipeline');
	await expect(page.getByRole('heading', { name: 'Pipeline' })).toBeVisible();
	await fire(page, 'Run cascade', '.banner.ok', 'run-abc');
	const posted = await calls(page);
	expect(posted.at(-1)).toMatchObject({ method: 'POST', path: '/produce' });
});

test('Request training posts the model + parsed feature datasets', async ({ page }) => {
	await seed(page, {
		'POST /train': { status: 202, body: { status: 'published', token: 'train-1', model: 'churn' } },
	});
	await page.goto('/lakehouse/models/pipeline');
	// Filling the inputs is itself the hydration proof here: `Request training` stays disabled until the
	// bound `model` state has a value, so it only becomes clickable once the component is live.
	await page.getByLabel('Model name').fill('churn');
	await page.getByLabel('Feature datasets').fill('silver$a, silver$b');
	await expect(page.getByRole('button', { name: 'Request training' })).toBeEnabled();
	await page.getByRole('button', { name: 'Request training' }).click();
	await expect(page.locator('.banner.ok')).toContainText('train-1');
	const body = (await calls(page)).at(-1)?.body as { model?: string; features?: unknown };
	expect(body?.model).toBe('churn');
	expect(body?.features).toEqual([{ dataset: 'silver$a' }, { dataset: 'silver$b' }]);
});

test('a 403 renders the project-admin denial banner', async ({ page }) => {
	await seed(page, {
		'POST /produce': { status: 403, body: { detail: 'produce needs project admin' } },
	});
	await page.goto('/lakehouse/models/pipeline');
	await fire(page, 'Run cascade', '.banner.fail', 'project-admin rung');
});

test('the shared sidebar marks the Pipeline leaf active', async ({ page }) => {
	await page.goto('/lakehouse/models/pipeline');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Pipeline' })).toBeVisible();
});
