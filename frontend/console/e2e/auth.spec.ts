import { test, expect } from '@playwright/test';

// The auth-OFF preview: its `/config.json` is `{"signIn": false}`, so the console signs nobody in.
// The `/auth/*` pages must still exist and FAIL SAFE (back to the landing, never a 404), and the
// landing must show no auth affordance, the main-menu bar (no estate-admin entries: `me` resolves
// null) and its honest scaffold. The real sign-in round trip runs in the auth-ON previews against
// the mock issuer.

test('/auth/login is a fail-safe no-op back to the landing when sign-in is not configured', async ({
	page,
	baseURL,
}) => {
	// The page EXISTS (not a 404) and, auth-off, goes back to the landing rather than starting a flow
	// against a missing issuer.
	const res = await page.goto('/auth/login');
	expect(res?.status()).toBe(200);
	await expect(page).toHaveURL(`${baseURL}/`);
	// The h1 is the LAGOM wordmark now (#144): two flex spans around the peepo image, so the
	// accessible name computes with a separator space — match loosely, never `exact`.
	await expect(page.getByRole('heading', { name: /LA\s*GOM/ })).toBeVisible();
});

test('/auth/logout drops the session and goes back to the landing', async ({ page, baseURL }) => {
	const res = await page.goto('/auth/logout');
	expect(res?.status()).toBe(200);
	await expect(page).toHaveURL(`${baseURL}/`);
});

test('the MAIN MENU navbar carries Home and Projects for an anonymous visitor — no Lakehouse, no Settings', async ({
	page,
}) => {
	// The estate level (ruling 2026-08-03): standing here you choose what to work on, so the bar is the
	// main menu, and the in-project Lakehouse entry is absent.
	await page.goto('/');
	const nav = page.getByRole('navigation', { name: 'Pages' });
	await expect(nav).toBeVisible();
	await expect(nav.getByRole('link', { name: 'Home', exact: true })).toHaveAttribute('href', '/');
	await expect(nav.getByRole('link', { name: 'Projects', exact: true })).toHaveAttribute(
		'href',
		'/projects',
	);
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Lakehouse', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('button')).toHaveCount(0);
	await expect(nav.getByRole('link')).toHaveCount(2);
	// SETTINGS is the third main-menu entry and it is estate-admin ONLY: `me` resolves null here, so it
	// shows DISABLED (#143) with the sign-in reason, neither a link nor a button.
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	const settings = nav.locator('[aria-disabled="true"]', { hasText: 'Settings' });
	await expect(settings).toBeVisible();
	await expect(settings).toHaveAttribute('title', /sign in/i);
	await expect(nav.getByText('Access')).toHaveCount(0);
	await expect(page.locator('[data-slot="navigation-menu-viewport"]')).toHaveCount(0);
});

test('the landing is HOME — an honest scaffold, not the project gallery', async ({ page }) => {
	await page.goto('/');
	// The h1 is the LAGOM wordmark now (#144): two flex spans around the peepo image, so the
	// accessible name computes with a separator space — match loosely, never `exact`.
	await expect(page.getByRole('heading', { name: /LA\s*GOM/ })).toBeVisible();
	// IT SAYS WHAT IT IS. The insights are not wired, and a landing page that implied otherwise — or
	// showed invented numbers — would be worse than one that admits it. The badge is the claim.
	await expect(page.getByText('Scaffold — no insights wired yet')).toBeVisible();
	await expect(page.getByRole('region', { name: 'Estate insights' })).toBeVisible();
	// …and it is NOT the gallery any more. That surface, and the membership empty state that used to
	// greet a project-less visitor as their entire product, live on /projects — asserted as an
	// ABSENCE here so the two cannot quietly both render the list again.
	await expect(page.getByText('You are not a member of any project yet')).toHaveCount(0);
	await expect(page.getByText('sign-in is not configured on this stack')).toHaveCount(0);
	// Navigation lives in the TOPNAVBAR alone (2026-08-05): the Projects/Settings cards this page
	// used to render duplicated it — asserted as an ABSENCE so they cannot quietly return.
	await expect(page.locator('[data-slot="home-projects-card"]')).toHaveCount(0);
	await expect(page.locator('[data-slot="home-settings-card"]')).toHaveCount(0);
	// authEnabled is false → the sign-in affordance is gated off entirely (no dead login link on an
	// ungoverned stack) — neither a page prompt nor a navbar menu entry.
	await expect(page.getByRole('link', { name: 'Sign out' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
});
