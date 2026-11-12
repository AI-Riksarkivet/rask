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
	// The LAGOM wordmark: two flex spans around the peepo image, so its text computes with a
	// separator space — match loosely, never `exact`.
	await expect(
		page
			.locator('#main-content')
			.getByText(/LA\s*GOM/)
			.first(),
	).toBeVisible();
});

test('/auth/logout drops the session and goes back to the landing', async ({ page, baseURL }) => {
	const res = await page.goto('/auth/logout');
	expect(res?.status()).toBe(200);
	await expect(page).toHaveURL(`${baseURL}/`);
});

test("the sidebar is every page's one navigation; an anonymous visitor sees the estate-admin rows disabled, with no way in", async ({
	page,
}) => {
	await page.goto('/');
	const nav = page.getByRole('navigation', { name: 'Pages' });
	await expect(nav).toBeVisible();
	for (const [name, href] of [
		['Home', '/'],
		['Projects', '/projects'],
		['Warehouses', '/catalog/warehouses'],
		['Namespaces', '/catalog/namespaces'],
		['Tables', '/catalog/tables'],
		['Storage', '/catalog/storage'],
		['Promotions', '/catalog/promotions'],
		['Workbench', '/workbench'],
	] as const) {
		await expect(nav.getByRole('link', { name, exact: true })).toHaveAttribute('href', href);
	}
	// `me` resolves null here, so every estate-admin row is SHOWN disabled with the sign-in reason as
	// its description: neither a link nor a button, and none of Settings' children handed out.
	for (const name of ['Events', 'Streams', 'DLQ', 'Settings']) {
		await expect(nav.getByRole('link', { name, exact: true })).toHaveCount(0);
		const row = nav.locator('[aria-disabled="true"]', { hasText: name });
		await expect(row).toBeVisible();
		await expect(row).toHaveAccessibleDescription(/sign in/i);
	}
	await expect(page.locator('a[href^="/admin"], a[href^="/settings"]')).toHaveCount(0);
});

test('the landing is HOME — an honest scaffold, not the project gallery', async ({ page }) => {
	await page.goto('/');
	// The LAGOM wordmark: two flex spans around the peepo image, so its text computes with a
	// separator space — match loosely, never `exact`.
	await expect(
		page
			.locator('#main-content')
			.getByText(/LA\s*GOM/)
			.first(),
	).toBeVisible();
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
	await expect(page.getByRole('menuitem', { name: 'Sign out' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
});
