import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { calls, seed as seedFor } from './mock-client';

// THE PROJECTS VIEW TOGGLE — gallery or table, remembered per user.
//
// The behaviour worth pinning is not "clicking swaps the markup" (it obviously does); it is that the
// choice SURVIVES A RELOAD, and that it survives it the right way — read on the SERVER, so the first
// paint is already the chosen view rather than a gallery that blinks into a table. A toggle that only
// works in memory looks identical in a screenshot and is a different feature.
//
// It rides the catalog's `dock-layout` user-state document, the estate's ONE per-subject store, under
// its own key inside the shared `workbenches` map. The read-modify-write in `view-prefs.remote.ts` is
// load-bearing for the same reason and is asserted below: PUTting only this key would delete every
// dock layout the user owns.

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

test('the toggle switches views, and the choice SURVIVES a reload', async ({ page }) => {
	await seed(page, {
		'GET /v1/me': ME_WITH_PROJECTS,
		'GET /v1/projects': ESTATE,
		// No saved preference yet: the document exists but carries no projects-view key. This is the
		// "never chosen" path, which must render the DEFAULT rather than an error or an empty list.
		'GET /v1/user-state/dock-layout': { exists: true, value: { workbenches: {} } },
		'PUT /v1/user-state/dock-layout': { ok: true },
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

	// THE WRITE. Asserted through the mock's ledger rather than by trusting the UI: the toggle is
	// optimistic, so the view flips whether or not anything was ever sent.
	await expect
		.poll(async () => (await calls(page, token)).map((c) => `${c.method} ${c.path}`))
		.toContain('PUT /v1/user-state/dock-layout');
	const put = (await calls(page, token)).find((c) => c.method === 'PUT');
	expect(put?.body).toEqual({ workbenches: { 'projects-view': 'table' } });

	// THE RELOAD — the actual contract. The saved value now comes back from the store, and the page
	// must render the table on the FIRST paint. `page.reload()` re-runs the server load, so a value
	// only held in component state cannot survive this.
	await seed(page, {
		'GET /v1/user-state/dock-layout': {
			exists: true,
			value: { workbenches: { 'projects-view': 'table' } },
		},
	});
	await page.reload();
	await expect(table(page)).toHaveAttribute('aria-checked', 'true');
	await expect(page.getByRole('table')).toBeVisible();
});

test('saving a view PRESERVES the other workbenches in the shared document', async ({ page }) => {
	// The read-modify-write, pinned. The `dock-layout` document holds every saved workbench in the
	// estate; a naive PUT of just this key would delete them all, and the only visible symptom would be
	// someone's docked layout quietly reverting to defaults days later.
	await seed(page, {
		'GET /v1/me': ME_WITH_PROJECTS,
		'GET /v1/projects': ESTATE,
		'GET /v1/user-state/dock-layout': {
			exists: true,
			value: {
				workbenches: {
					'lakehouse-workbench': { grid: 'a-real-saved-layout' },
					'media-workbench': { grid: 'another-one' },
				},
			},
		},
		'PUT /v1/user-state/dock-layout': { ok: true },
	});
	await page.goto('/projects');
	await table(page).click();

	await expect
		.poll(async () => (await calls(page, token)).some((c) => c.method === 'PUT'))
		.toBe(true);
	const put = (await calls(page, token)).find((c) => c.method === 'PUT');
	expect(put?.body).toEqual({
		workbenches: {
			'lakehouse-workbench': { grid: 'a-real-saved-layout' },
			'media-workbench': { grid: 'another-one' },
			'projects-view': 'table',
		},
	});
});

test('an unreadable preference degrades to the default view, not to an error', async ({ page }) => {
	// Every failure path answers null, so the page renders the gallery. A preference is not data: a
	// store that cannot be read must cost the user their remembered view and nothing else.
	await seed(page, {
		'GET /v1/me': ME_WITH_PROJECTS,
		'GET /v1/projects': ESTATE,
		'GET /v1/user-state/dock-layout': { status: 500, body: { detail: 'state store down' } },
	});
	const res = await page.goto('/projects');
	expect(res?.status()).toBe(200);
	await expect(gallery(page)).toHaveAttribute('aria-checked', 'true');
	await expect(page.getByRole('link', { name: 'acme' })).toBeVisible();
});

test('the toggle is absent when there is nothing to arrange', async ({ page }) => {
	// A member with no projects gets the empty state and no view control — offering two ways to look
	// at nothing is noise, and the empty state is the answer to the question they actually have.
	await seed(page, {
		'GET /v1/me': { ...ME_ADMIN, estate_admin: false, projects: [] },
		'GET /v1/user-state/dock-layout': { exists: true, value: { workbenches: {} } },
	});
	await page.goto('/projects');
	await expect(page.getByText('You are not a member of any project yet')).toBeVisible();
	await expect(gallery(page)).toHaveCount(0);
	await expect(table(page)).toHaveCount(0);
});
