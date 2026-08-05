import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { seed as seedFor } from './mock-client';

// `/settings` — the estate's configuration surface and the third place in the main menu.
//
// Two things are worth pinning, and only one of them is about the page: that the ROUTE exists and
// carries what the ruling put there, and that its gate is SERVER-SIDE. The navbar hides Settings from
// a non-admin, but hiding a link is presentation, not authorization — anyone can type the URL, and a
// gate that lives only in the bar is not a gate.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

const nav = (page: Page) => page.getByRole('navigation', { name: 'Zones' });

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('an estate admin gets Settings in the bar, and the page carries the estate’s configuration', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(200);
	await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();

	// The three auth/authz rows the ruling moved here out of the Lakehouse panel. They are CROSS-ZONE
	// — the pages are still served by the lakehouse app — so each must hard-navigate or a soft nav
	// lands in a route this zone does not own (404). That attribute is the whole reason to assert
	// hrefs here rather than trust the markup.
	for (const [name, href] of [
		['Access', '/lakehouse/governance/access'],
		['Tenants', '/lakehouse/admin/tenants'],
		['Audit', '/lakehouse/governance/audit'],
	] as const) {
		const link = page.getByRole('link', { name: new RegExp(`^${name}`) });
		await expect(link).toHaveAttribute('href', href);
		await expect(link).toHaveAttribute('data-sveltekit-reload', '');
	}

	// The unwired half says so rather than pretending. A settings form that silently discards what you
	// type is worse than one that admits it cannot save yet.
	for (const row of ['Notifications', 'New-project defaults', 'Credentials']) {
		await expect(page.getByText(row, { exact: true })).toBeVisible();
	}
	await expect(page.getByText('Not wired').first()).toBeVisible();

	// …and the bar here is still the MAIN MENU, because /settings is the estate level.
	await expect(nav(page).getByRole('link', { name: 'Home', exact: true })).toBeVisible();
	await expect(nav(page).getByRole('link', { name: 'Projects', exact: true })).toBeVisible();
});

test('a non-admin cannot reach /settings by typing the URL — the gate is on the SERVER', async ({
	page,
}) => {
	// The assertion that matters. `estate_admin: false` resolves fine, so this is not an auth failure;
	// it is an authorization one, and the answer is 404 rather than 403 on purpose: someone who may not
	// configure the estate should not learn from the response that estate configuration lives here.
	await seed(page, {
		'GET /v1/me': { ...ME_ADMIN, estate_admin: false, projects: [] },
	});
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(404);
	// …and the bar never named it either — fail-closed on both surfaces, not just the server.
	await page.goto('/projects');
	await expect(nav(page).getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav(page).getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
});

test('an UNRESOLVED identity gets 403, not 404 — a broken lookup is not a missing page', async ({
	page,
}) => {
	// The distinction the gate draws deliberately. The catalog could not say who this is, which is a
	// different failure from "you may not": answering 404 would send someone chasing a dead link when
	// their session, or the catalog, is the actual problem.
	await seed(page, {
		'GET /v1/me': { status: 503, body: { detail: 'catalog unavailable' } },
	});
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(403);
});
