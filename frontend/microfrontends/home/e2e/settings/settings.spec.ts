import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// `/settings` — the PLATFORM configuration surface and the third place in the main menu.
//
// Two things are worth pinning, and only one of them is about the page: that the ROUTE exists and
// carries what the ruling put there, and that its gate is SERVER-SIDE. The navbar SHOWS Settings to a
// non-admin, disabled and with its reason (#143) — and either way the bar is presentation, not
// authorization: anyone can type the URL, and a gate that lives only in the bar is not a gate. Since
// #105 the gate is a `+layout.server.ts`, so it covers `/settings/access` and `/settings/audit` too —
// a door that has to be remembered per child is a door that will be forgotten.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

const nav = (page: Page) => page.getByRole('navigation', { name: 'Zones' });

/** The page's OWN rows — scoped to its sections, never the whole document. The navbar also carries a
 *  "Projects" link (it is a main-menu entry AND a settings row now), so an unscoped `getByRole('link')`
 *  matches two elements and fails strict mode. Scoping is also the more honest assertion: this file is
 *  about what the settings PAGE offers, not about the chrome above it. */
const rows = (page: Page) => page.locator('section[aria-labelledby^="settings-"]');

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

	// The three platform rows. They are SAME-ZONE now (#105 ported the pages here; `/projects` was
	// always this app's), so each must be an ordinary soft link — and the ABSENCE of
	// `data-sveltekit-reload` is asserted, not merely unmentioned: the attribute is harmless to a gate
	// but costs a full document load on every click, which is exactly the cost the port removed.
	for (const [name, href] of [
		['Users & roles', '/settings/access'],
		['Projects', '/projects'],
		['Audit', '/settings/audit'],
	] as const) {
		const link = rows(page).getByRole('link', { name: new RegExp(`^${name}`) });
		await expect(link).toHaveAttribute('href', href);
		await expect(link).not.toHaveAttribute('data-sveltekit-reload', '');
	}

	// The unwired half says so rather than pretending. A settings form that silently discards what you
	// type is worse than one that admits it cannot save yet.
	for (const row of ['New-project defaults', 'Credentials']) {
		await expect(page.getByText(row, { exact: true })).toBeVisible();
	}
	await expect(page.getByText('Not wired').first()).toBeVisible();

	// …and the bar here is still the MAIN MENU, because /settings is the estate level.
	await expect(nav(page).getByRole('link', { name: 'Home', exact: true })).toBeVisible();
	await expect(nav(page).getByRole('link', { name: 'Projects', exact: true })).toBeVisible();
});

test('a non-admin is REFUSED /settings with the reason — the gate is on the SERVER (#143)', async ({
	page,
}) => {
	// The assertion that matters. `estate_admin: false` resolves fine, so this is not an auth failure,
	// it is an authorization one — and the answer is 403 NAMING THE RELATION.
	//
	// This was 404, deliberately: "someone who may not configure the estate should not learn from the
	// response that estate configuration lives here." The owner ruling (#143) overrides that, and the
	// trade is worth stating plainly rather than quietly losing. GIVEN UP: a non-admin can now confirm
	// /settings exists. BOUGHT: they can find out WHY they cannot open it, and which relation to ask
	// for. The disclosure was already thin — the estate ships a documented settings surface and the
	// navbar names it — while the 404 cost every legitimate user their next step. Reverting means
	// reverting the ruling, not just this line.
	await seed(page, {
		'GET /v1/me': { ...ME_ADMIN, estate_admin: false, projects: [] },
	});
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(403);
	// The reason is the FGA denial's own wording, not a generic "forbidden" — that is the half of the
	// ruling a bare status code would silently drop.
	await expect(page.getByText('can_observe_events', { exact: false })).toBeVisible();

	// …and the bar SHOWS it, disabled — never omitted. A link would be reachable by keyboard and by
	// paste however it is painted, so the disabled entry must NOT be a link.
	await page.goto('/projects');
	await expect(nav(page).getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	const disabled = nav(page).locator('[aria-disabled="true"]', { hasText: 'Settings' });
	await expect(disabled).toBeVisible();
	await expect(disabled).toHaveAttribute('title', /can_observe_events/);
});

test('an UNRESOLVED identity is refused for a DIFFERENT reason than a non-admin', async ({
	page,
}) => {
	// The distinction the gate draws deliberately. The catalog could not say who this is, which is a
	// different failure from "you may not": conflating them sends someone hunting for a grant when
	// their session, or the catalog, is the actual problem.
	//
	// Both are 403 since #143 (the non-admin case stopped being a 404), so the STATUS no longer carries
	// the distinction and the MESSAGE is the only thing that does. That is exactly why this asserts the
	// wording: drop it and the two failures become indistinguishable to the person reading the screen.
	await seed(page, {
		'GET /v1/me': { status: 503, body: { detail: 'catalog unavailable' } },
	});
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(403);
	await expect(page.getByText('could not be confirmed', { exact: false })).toBeVisible();
	await expect(page.getByText('can_observe_events', { exact: false })).toHaveCount(0);
});

// The CHILDREN, and this is the assertion the port had to add rather than inherit. The gate used to be
// `settings/+page.server.ts`, which covered exactly one URL: `/settings/access` reads the platform's
// whole tuple store and `/settings/audit` its cross-tenant trail, so an ungated child would have served
// both to anyone who typed the path. Hoisting it to `+layout.server.ts` is what makes the door
// structural, and these two tests are what keep it that way — a future child route inherits the gate,
// and if someone un-hoists it, this fails rather than the next incident.
for (const child of ['/settings/access', '/settings/audit'] as const) {
	test(`${child} is behind the SAME server-side door as /settings`, async ({ page }) => {
		await seed(page, { 'GET /v1/me': { ...ME_ADMIN, estate_admin: false, projects: [] } });
		expect((await page.goto(child))?.status()).toBe(403);
		// Inheriting the STATUS is not enough — a child that inherited the door but grew its own
		// generic message would satisfy a status-only assertion while losing the ruling.
		await expect(page.getByText('can_observe_events', { exact: false })).toBeVisible();
	});

	test(`${child} answers 403 for an unresolved identity, like its parent`, async ({ page }) => {
		await seed(page, { 'GET /v1/me': { status: 503, body: { detail: 'catalog unavailable' } } });
		expect((await page.goto(child))?.status()).toBe(403);
		await expect(page.getByText('could not be confirmed', { exact: false })).toBeVisible();
	});
}
