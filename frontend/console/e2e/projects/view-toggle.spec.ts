import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { calls, seed as seedFor } from '../mock-client';

// THE PROJECTS VIEW TOGGLE — gallery or table.
//
// No door on this stack keeps per-person state, so the toggle switches the view for the page it is on
// and remembers nothing: no request goes anywhere for it, nothing lands in the reader's activity log,
// and a reload paints the default again. Pinned so a console that starts calling a door the catalog does not serve fails here,
// rather than as a 404 on the estate that a mock would hide.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

const ME_WITH_PROJECTS = {
	...ME_ADMIN,
	projects: [
		{ project: 'acme', role: 'admin' as const },
		{ project: 'beta', role: 'member' as const },
	],
};

/** The estate listing the admin gallery reads. Two rows, so both views have something to render. */
const ESTATE = [
	{ project: 'acme', warehouses: [{ id: 'acme-wh' }] },
	{ project: 'beta', warehouses: [] },
];

const gallery = (page: Page) => page.locator('[data-slot="projects-view-gallery"]');
const table = (page: Page) => page.locator('[data-slot="projects-view-table"]');

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('the toggle switches views, sends nothing, and a reload paints the default', async ({
	page,
}) => {
	await seed(page, {
		'GET /management/v1/me': ME_WITH_PROJECTS,
		'GET /management/v1/projects': ESTATE,
	});
	const sent: string[] = [];
	page.on('request', (req) => {
		const path = new URL(req.url()).pathname;
		if (path.startsWith('/api/') && req.method() !== 'GET') sent.push(`${req.method()} ${path}`);
	});
	await page.goto('/projects');

	// Default is the gallery: cards, no table.
	await expect(gallery(page)).toHaveAttribute('aria-checked', 'true');
	await expect(page.getByRole('table')).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'acme' })).toBeVisible();

	await table(page).click();

	// Same projects, denser surface — a real table with a row per project, not a re-skinned grid.
	await expect(table(page)).toHaveAttribute('aria-checked', 'true');
	await expect(gallery(page)).toHaveAttribute('aria-checked', 'false');
	await expect(page.getByRole('table')).toBeVisible();
	await expect(page.getByRole('cell', { name: 'acme' })).toBeVisible();
	await expect(page.getByRole('cell', { name: 'beta' })).toBeVisible();
	// The row still reaches the project — the two views differ in density, not in destination.
	await expect(page.getByRole('link', { name: 'acme', exact: true })).toHaveAttribute(
		'href',
		'/projects/acme',
	);

	// A view change is not a write: the bell has nothing to count.
	await expect(page.getByRole('button', { name: /^Activity/ })).toHaveAccessibleName('Activity');

	await page.reload();
	await expect(gallery(page)).toHaveAttribute('aria-checked', 'true');
	expect(sent).toEqual([]);
	expect(await calls(page, token)).toEqual([]);
});

test('the toggle is absent when there is nothing to arrange', async ({ page }) => {
	// A member with no projects gets the empty state and no view control — offering two ways to look
	// at nothing is noise, and the empty state is the answer to the question they actually have.
	await seed(page, {
		'GET /management/v1/me': { ...ME_ADMIN, estate_admin: false, projects: [] },
	});
	await page.goto('/projects');
	await expect(page.getByText('You are not a member of any project yet')).toBeVisible();
	await expect(gallery(page)).toHaveCount(0);
	await expect(table(page)).toHaveCount(0);
});
