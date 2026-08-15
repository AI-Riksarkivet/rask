import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// #143 — "every gated action SHOWS disabled with its denial reason, never hidden."
//
// The estate violated the ruling in three distinct shapes, and this file exercises all three because
// fixing one and calling it done is how the other two survive: a nav ENTRY (the main menu's Settings),
// an action BUTTON (the gallery's New project), and the ROUTE behind them (which answered 404).
//
// It also writes the screenshots the owner reviews. Those are evidence, not assertions — every shot
// sits next to the check it illustrates, so a silently-broken surface fails the run rather than
// producing a nice picture of the wrong thing.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

const nav = (page: Page) => page.getByRole('navigation', { name: 'Zones' });

const NON_ADMIN = { ...ME_ADMIN, estate_admin: false, projects: [] };
const SHOTS = 'e2e/__shots__';

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('the main menu: an admin gets a live Settings, a non-admin gets it DISABLED', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN });
	await page.goto('/projects');
	// A BUTTON, not a link: Settings carries panel rows, so an entitled caller gets the NavigationMenu
	// trigger that opens them. (Asserting a link here fails — which is the useful shape of this test:
	// the admin and non-admin renderings differ in kind, not just in styling.)
	await expect(nav(page).getByRole('button', { name: 'Settings', exact: true })).toBeVisible();
	await expect(nav(page).locator('[aria-disabled="true"]', { hasText: 'Settings' })).toHaveCount(0);
	await nav(page).screenshot({ path: `${SHOTS}/143-navbar-admin.png` });

	// The SAME bar for a non-admin: the entry does not move and does not vanish, it goes inert.
	await seed(page, { 'GET /v1/me': NON_ADMIN });
	await page.goto('/projects');
	const denied = nav(page).locator('[aria-disabled="true"]', { hasText: 'Settings' });
	await expect(denied).toBeVisible();
	// Neither a link nor a button: it must not be clickable, reachable as a control, or paste-able.
	await expect(nav(page).getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav(page).getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	// The reason names the RELATION — the half that turns a dead end into a request someone can make.
	await expect(denied).toHaveAttribute('title', /can_observe_events/);
	await nav(page).screenshot({ path: `${SHOTS}/143-navbar-nonadmin.png` });
});

test('the disabled entry hands out NO panel rows — shown, but not opened into', async ({ page }) => {
	// The fail-closed half, and the one a careless refactor loses. Settings carries a panel whose rows
	// reach /settings/access — the platform's whole tuple store. Showing the entry must not show those.
	await seed(page, { 'GET /v1/me': NON_ADMIN });
	await page.goto('/projects');
	const denied = nav(page).locator('[aria-disabled="true"]', { hasText: 'Settings' });
	await denied.click({ force: true });
	await expect(nav(page).getByRole('link', { name: /Users & roles/ })).toHaveCount(0);
	await expect(page.locator('a[href="/settings/access"]')).toHaveCount(0);
});

test('the gallery: New project is SHOWN disabled to a non-admin, and does not fire', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/me': NON_ADMIN });
	await page.goto('/projects');

	const gate = page.locator('[data-slot="gated-action"]');
	await expect(gate).toHaveAttribute('aria-disabled', 'true');
	// The BUTTON is still rendered inside it. That is the ruling: the affordance is visible, so the
	// reader learns the capability exists and that THEY lack it — not that the product lacks it.
	const button = gate.getByRole('button', { name: /New project/ });
	await expect(button).toBeVisible();
	await page.screenshot({ path: `${SHOTS}/143-gallery-nonadmin.png` });

	// …and it is inert. The capture-phase guard runs before the child's own onclick, so the create
	// dialog must not open. `force` because the control is deliberately not hit-testable.
	await button.click({ force: true });
	await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('the gallery: an ADMIN gets the same button, live', async ({ page }) => {
	// The other side of the gate, and the reason `allowed` renders children untouched: an entitled
	// caller must not pay for the refused case with a wrapper that changes geometry or behaviour.
	await seed(page, { 'GET /v1/me': ME_ADMIN });
	await page.goto('/projects');
	await expect(page.locator('[data-slot="gated-action"]')).toHaveCount(0);
	await page.getByRole('button', { name: /New project/ }).click();
	await expect(page.getByRole('dialog')).toBeVisible();
	await page.screenshot({ path: `${SHOTS}/143-gallery-admin.png` });
});

test('the route: a non-admin is refused /settings WITH the reason, not a 404', async ({ page }) => {
	await seed(page, { 'GET /v1/me': NON_ADMIN });
	const res = await page.goto('/settings');
	expect(res?.status()).toBe(403);
	await expect(page.getByText('can_observe_events', { exact: false })).toBeVisible();
	await page.screenshot({ path: `${SHOTS}/143-settings-403.png` });
});
