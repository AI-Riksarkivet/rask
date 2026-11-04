import { test, expect } from '@playwright/test';
import { mockMe, SESSION_NAME, signIn } from './session';

// Sign-in, under the SIGN-IN-FIRST rule, against the mock issuer (`e2e/mock-oidc.ts`). It asserts
// (1) a signed-out visit goes to the issuer's authorize endpoint with a PKCE challenge and the console's
// own callback, and nothing renders meanwhile; (2) signed IN, the round trip comes back to the page
// that was asked for and the shared nav-user renders the session identity; (3) the token rides as the
// bearer of the console's own API calls.

test('signed-out: a page visit goes to the issuer with PKCE, and back to the console callback', async ({
	page,
	baseURL,
}) => {
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'Mock IdP login' })).toBeVisible();
	const authorize = new URL(page.url());
	expect(authorize.pathname).toBe('/oidc/auth');
	expect(authorize.searchParams.get('response_type')).toBe('code');
	expect(authorize.searchParams.get('client_id')).toBe('rask-web');
	expect(authorize.searchParams.get('code_challenge_method')).toBe('S256');
	expect(authorize.searchParams.get('code_challenge')).toBeTruthy();
	expect(authorize.searchParams.get('redirect_uri')).toBe(`${baseURL}/auth/callback`);
});

test('signed-in: the shared nav-user shows the session identity when auth is enabled', async ({
	context,
	page,
}) => {
	await signIn(context);
	await mockMe(page); // estate-admin identity: the admin layout door opens
	// Any gated page of the console proves the seam; the audit trail used to be the specimen and left
	// with #105. The DLQ is the cheapest surviving one — no observability upstream to stand in for.
	await page.goto('/admin/dlq');
	// Back from the issuer, on the page that was asked for.
	await expect(page.getByRole('heading', { name: 'Lineage DLQ' })).toBeVisible();
	await expect(page).toHaveURL(/\/admin\/dlq$/);
	// The navbar profile dropdown renders the signed-in name — the id token's claims flowed
	// session→user→AppShell→TopNavbar→nav-user and selected the signed-in branch.
	await page.getByRole('button', { name: 'Account' }).click();
	await expect(page.getByText(SESSION_NAME)).toBeVisible();
});

test('signed-in: the console API calls carry the issued token as their bearer', async ({
	context,
	page,
}) => {
	await signIn(context, { token: 'e2e-token:admin:bearer-check' });
	const me = page.waitForRequest('**/api/catalog/management/v1/me');
	await page.goto('/catalog/tables');
	expect((await me).headers()['authorization']).toBe('Bearer e2e-token:admin:bearer-check');
});
