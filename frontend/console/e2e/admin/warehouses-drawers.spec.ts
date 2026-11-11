import { test, expect, type Page } from '@playwright/test';
import { ME_MEMBER, mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// Row-drawer coverage for the WAREHOUSE registry (goal cond 8): clicking a row opens the bits-ui
// drawer with the full record + jump links, and the in-cell link/action button stopPropagation so they
// keep their own behavior without also opening it. The registry is seeded on the mock catalog per
// bearer.

const WAREHOUSES = [
	{
		id: 'acme-wh',
		project: 'acme',
		bucket: 'acme-bucket',
		root_uri: 's3://acme-bucket',
		status: 'active',
		created_at: '2026-07-01T00:00:00Z',
	},
	{
		id: 'acme-gold',
		project: 'acme',
		bucket: 'acme-gold-bucket',
		root_uri: 's3://acme-gold-bucket',
		status: 'active',
		serving: 'gold',
	},
];

let token: string;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, { 'GET /management/v1/warehouses': WAREHOUSES });
});

test('a warehouse row opens the full record with the serving marker + project jump link', async ({
	page,
}) => {
	await page.goto('/catalog/warehouses');
	// the gold serving record: click its PROJECT cell (id is a link, actions is a button)
	await page
		.locator('tbody tr', { hasText: 'acme-gold-bucket' })
		.getByText('acme', { exact: true })
		.click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('acme-gold');
	await expect(drawer).toContainText('s3://acme-gold-bucket');
	// the serving-class marker — a gold warehouse is stated as such, a work one is not
	await expect(drawer.getByText('serving · gold')).toBeVisible();
	// The project jump goes UP to the estate level: a project is the top of the hierarchy, so its page
	// is `/projects/<p>`.
	const jump = drawer.getByRole('link', { name: 'Open project' });
	await expect(jump).toHaveAttribute('href', '/projects/acme');
});

test('a work warehouse row states the work class honestly (no serving marker)', async ({
	page,
}) => {
	await page.goto('/catalog/warehouses');
	await page
		.locator('tbody tr', { hasText: 'acme-bucket' })
		.first()
		.getByText('acme', { exact: true })
		.click();
	const drawer = page.getByRole('dialog');
	await expect(drawer).toContainText('acme-wh');
	await expect(drawer).toContainText('work warehouse');
	await expect(drawer.getByText('serving · gold')).toHaveCount(0);
	await expect(drawer).toContainText('2026-07-01T00:00:00Z');
});

test('Enter on a row action opens only that action, and the skip link reaches the content', async ({
	page,
}) => {
	await page.goto('/catalog/warehouses');
	await expect(page.getByRole('link', { name: 'acme-gold' })).toBeVisible();
	// The first stop on the page skips the sidebar.
	await page.keyboard.press('Tab');
	const skip = page.getByRole('link', { name: 'Skip to content' });
	await expect(skip).toBeFocused();
	await page.keyboard.press('Enter');
	await expect(page.locator('#main-content')).toBeFocused();

	await page.getByRole('button', { name: 'Delete warehouse acme-wh', exact: true }).focus();
	await page.keyboard.press('Enter');
	// One dialog: the delete. The row's own drawer stays shut.
	const dialogs = page.getByRole('dialog');
	await expect(dialogs).toHaveCount(1);
	await expect(dialogs).toContainText('acme-wh');
	await expect(page.getByText('The full warehouse registry record.')).toHaveCount(0);
});

test('a person who is not an estate admin never reads the estate-only events feed', async ({
	context,
	page,
}, testInfo) => {
	token = `${TOKEN.member}:${testInfo.testId}`;
	await context.clearCookies();
	await signIn(context, { token });
	await mockMe(page, ME_MEMBER);
	await seed(page, { 'GET /management/v1/warehouses': WAREHOUSES });
	const polled: string[] = [];
	page.on('request', (req) => {
		const path = new URL(req.url()).pathname;
		if (path.endsWith('/management/v1/events')) polled.push(path);
	});
	await page.goto('/catalog/warehouses');
	await expect(page.getByRole('link', { name: 'acme-gold' })).toBeVisible();
	// Longer than one poll of the control cursor: a cursor opened for this person would have asked.
	await page.waitForTimeout(6_000);
	// The admin area's refusal page reads nothing either.
	await page.goto('/admin');
	await expect(page.getByText('Admin is estate-admin only')).toBeVisible();
	expect(polled).toEqual([]);
});
