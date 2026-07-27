import { test, expect } from '@playwright/test';
import { mockMe, SESSION_NAME, signIn } from './session';

// The shared cross-zone auth seam (P5 "auth in every MFE"), under the LOGIN-FIRST gate. This suite runs
// the admin zone with OIDC CONFIGURED (playwright.config.ts webServer.env). It asserts (1) a signed-out
// page navigation is redirected to the home zone's /auth/login carrying the ?redirect= return contract —
// the login page is the first thing a signed-out user sees; (2) API routes are exempt (fetch clients keep
// their JSON status contract, no corrupting redirect); (3) signed IN (dev-grade unsealed cookie — no
// SESSION_SECRET on the dev server), the shared nav-user renders the session identity, proving
// session→user→AppShell→nav-user flowed; (4) /auth/* stays outside the domain zones (home owns it).

test('signed-out: a page navigation redirects to /auth/login?redirect=<original path>', async ({
	page,
}) => {
	// The gate answers 302 → /auth/login, which the home zone owns; in this isolated admin server the
	// follow-up 404s, so pin the CONTRACT on the un-followed redirect response itself.
	const res = await page.request.get('/lakehouse/admin/audit', {
		maxRedirects: 0,
		headers: { accept: 'text/html' },
	});
	expect(res.status()).toBe(302);
	expect(res.headers()['location']).toBe('/auth/login?redirect=%2Flakehouse%2Fadmin%2Faudit');
});

test('signed-out: API routes are EXEMPT — fetch clients get their status/JSON, never a redirect', async ({
	page,
}) => {
	const res = await page.request.get('/lakehouse/api/projects', { maxRedirects: 0 });
	expect(res.status()).not.toBe(302); // the BFF answers itself (here 500/4xx without a backend — not a redirect)
});

test('signed-in: the shared nav-user shows the session identity when auth is enabled', async ({
	context,
	page,
}) => {
	await signIn(context);
	await mockMe(page); // estate-admin identity: the admin layout door opens
	await page.route('**/api/audit**', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: '{"events":[]}' }),
	);
	await page.goto('/lakehouse/admin/audit');
	await expect(page.getByRole('heading', { name: 'Audit log' })).toBeVisible();
	// The navbar profile dropdown renders the minted session's name — proof that the cookie session
	// flowed session→user→AppShell→TopNavbar→nav-user and selected the signed-in branch.
	await page.getByRole('button', { name: 'Account' }).click();
	await expect(page.getByText(SESSION_NAME)).toBeVisible();
});

test('/auth was relocated OUT of the domain zones — the home zone owns it (admin 404s)', async ({
	context,
	page,
}) => {
	// Signed in so the login-first gate doesn't intercept: only the home zone serves /auth/*; a direct hit
	// inside the isolated admin dev server 404s. The ingress path-routes /auth → home in the composed
	// deployment (P5 chart).
	await signIn(context);
	const res = await page.goto('/lakehouse/admin/auth/login');
	expect(res?.status()).toBe(404);
});
