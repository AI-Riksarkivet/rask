import { test, expect } from '@playwright/test';

// The home zone owns the relocated OIDC BFF routes (/auth/{login,callback,logout}) AND the
// signed-in landing (navbar + project gallery). Running auth-OFF (no OIDC env, no catalog), the
// routes must exist and FAIL SAFE — login/logout are no-op redirects home, never 404s or 500s —
// and the landing must show no auth affordance, the base navbar (no estate-admin entries: fetchMe
// resolves null), and the gallery's empty state. The real per-user round-trip (login → Dex →
// callback → sealed cookie → cross-zone session) is the live P5 drive.

test('GET /auth/login is a fail-safe no-op redirect home when auth is unconfigured', async ({
	page,
	baseURL,
}) => {
	// The route EXISTS in the home zone (not a 404) and, auth-off, redirects home rather than erroring or
	// starting a flow against a missing IdP. Following the 302 lands on the home landing.
	const res = await page.goto('/auth/login');
	expect(res?.status()).toBe(200); // followed the 302 → the landing rendered (not a 404/500)
	expect(page.url()).toBe(`${baseURL}/`);
	await expect(page.getByRole('heading', { name: 'lance', exact: true })).toBeVisible();
});

test('GET /auth/logout clears the session and redirects home', async ({ page, baseURL }) => {
	const res = await page.goto('/auth/logout');
	expect(res?.status()).toBe(200);
	expect(page.url()).toBe(`${baseURL}/`);
});

test('the navbar carries one trigger per zone and no governance column for an anonymous visitor', async ({
	page,
}) => {
	await page.goto('/');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav).toBeVisible();
	// One trigger per ZONE: Lakehouse (catalog + models + lineage + governance) and Search (the media
	// read plane) carry sub-areas, so they are NavigationMenu triggers opening a panel; Annotate is a
	// single-surface zone and stays a plain link. Home is the product mark, not an entry at all.
	for (const domain of ['Lakehouse', 'Search']) {
		await expect(nav.getByRole('button', { name: domain, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('link', { name: 'Annotate', exact: true })).toBeVisible();
	// Two TRIGGERS (Lakehouse, Search) — Annotate is a plain link, not a button, because that
	// zone has a single surface and a one-row dropdown would be noise. Compute joins as a third
	// trigger when the rask zone lands.
	await expect(nav.getByRole('button')).toHaveCount(2);
	await expect(nav.getByRole('link', { name: 'Home', exact: true })).toHaveCount(0);
	// fetchMe resolves null (no session, no catalog) → estate_admin is unknowable → fail-closed.
	// Access has no home outside the estate-admin Governance column, so it is nowhere in this bar…
	await expect(nav.getByText('Access')).toHaveCount(0);
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	// This zone SSRs the resolved triggers (fetchMe has nothing to wait for auth-off), so the buttons are
	// in the markup before the client bundle has attached bits-ui's handlers — clicking straight away
	// lands on inert HTML and the panel silently never opens.
	//
	// That wait used to be `networkidle`, and it can never fire again: every zone's shell now holds a
	// LIVE query open for the notification bell, so this app has no idle network BY DESIGN. Waiting for
	// the button to exist would not help either — it exists in the server-rendered HTML, before any
	// listener is attached. The honest condition is "clicking this actually opens the panel", so that is
	// what is asserted: click, and if nothing happened yet, click again until it does.
	await expect(async () => {
		await nav.getByRole('button', { name: 'Lakehouse', exact: true }).click();
		await expect(panel).toBeVisible({ timeout: 1000 });
	}).toPass({ timeout: 20_000 });
	// …and opening the one trigger that WOULD carry them proves the columns never rendered.
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
	// Every panel row leaves the home zone's route manifest, so it must hard-navigate. From HOME that
	// is still true of every row, including the lakehouse ones — home is its own zone.
	await expect(panel.getByRole('link', { name: /^Registry/ })).toHaveAttribute(
		'href',
		'/lakehouse/models',
	);
	await expect(panel.locator('a[href="/lakehouse/models"]')).toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});

test('the landing shows the gallery empty state and NO auth control when auth is off', async ({
	page,
}) => {
	await page.goto('/');
	await expect(page.getByRole('heading', { name: 'lance', exact: true })).toBeVisible();
	// Auth-off + no catalog → signed out, no projects → the empty state names the unconfigured sign-in.
	await expect(page.getByText('sign-in is not configured on this stack')).toBeVisible();
	// authEnabled is false → the sign-in affordance is gated off entirely (no dead login link on an
	// ungoverned stack) — neither the gallery prompt nor a navbar menu entry.
	await expect(page.getByRole('link', { name: 'Sign out' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
});
