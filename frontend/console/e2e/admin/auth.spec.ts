import { test, expect } from '@playwright/test';
import { issuerBehaviour, mockMe, SESSION_NAME, signIn, TOKEN, withoutEndSession } from './session';

// Sign-in, under the SIGN-IN-FIRST rule, against the mock issuer (`e2e/mock-oidc.ts`). It asserts
// (1) a signed-out visit goes to the issuer's authorize endpoint with a PKCE challenge and the console's
// own callback, and nothing renders meanwhile; (2) signed IN, the round trip comes back to the page
// that was asked for and the shared nav-user renders the session identity; (3) the token rides as the
// bearer of the console's own API calls; (4) signing out ends the issuer's session with the id token
// as its hint; (5) a session is renewed with its refresh token without leaving the page, before its
// token expires and when a door answers 401, and one that cannot be renewed keeps the page and says so;
// (6) a sign-in the issuer refuses says why.

test('signed-out: a page visit goes to the issuer with PKCE, and back to the console callback', async ({
	page,
	baseURL,
}) => {
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'Mock IdP login' })).toBeVisible();
	const authorize = new URL(page.url());
	expect(authorize.pathname).toBe('/oidc/auth');
	expect(authorize.searchParams.get('response_type')).toBe('code');
	expect(authorize.searchParams.get('client_id')).toBe('rask-console');
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
	// Any gated page of the console proves the seam. The DLQ is the cheapest: no observability upstream
	// to stand in for.
	await page.goto('/admin/dlq');
	// Back from the issuer, on the page that was asked for.
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();
	await expect(page).toHaveURL(/\/admin\/dlq$/);
	// The account menu renders the signed-in name — the id token's claims flowed
	// session→user→AppShell→NavbarUser and selected the signed-in branch.
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

test('sign-out ends the issuer session with the id token as its hint', async ({
	context,
	page,
	baseURL,
}) => {
	await signIn(context);
	await mockMe(page);
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: 'Sign out' }).click();

	await expect(page.getByRole('heading', { name: 'Mock IdP signed out' })).toBeVisible();
	const endSession = new URL(page.url());
	expect(endSession.pathname).toBe('/oidc/logout');
	expect(endSession.searchParams.get('id_token_hint')).toMatch(/^[\w-]+\.[\w-]+\.$/);
	expect(endSession.searchParams.get('post_logout_redirect_uri')).toBe(`${baseURL}/`);
});

test('sign-out without an issuer end-session endpoint makes the next sign-in re-authenticate', async ({
	context,
	page,
}) => {
	await signIn(context);
	await withoutEndSession(context);
	await mockMe(page);
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: 'Sign out' }).click();

	// The issuer still holds the session (the identity cookie), and only `prompt=login` keeps it from
	// signing the person straight back in.
	await expect(page.getByRole('heading', { name: 'Mock IdP login' })).toBeVisible();
	const authorize = new URL(page.url());
	expect(authorize.pathname).toBe('/oidc/auth');
	expect(authorize.searchParams.get('prompt')).toBe('login');
});

test('the account menu opens on its first item, and Sign out is a menu item beneath it', async ({
	context,
	page,
}) => {
	await signIn(context);
	await mockMe(page);
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();
	await page.getByRole('button', { name: 'Account' }).press('Enter');
	const signOut = page.getByRole('menuitem', { name: 'Sign out' });
	await expect(signOut).toBeVisible();
	await expect(page.getByRole('menuitem').first()).toBeFocused();
	await expect(signOut).not.toBeFocused();
});

test('signing out is an act, not a page: a link to /auth/logout ends nobody’s session', async ({
	context,
	page,
}) => {
	await signIn(context);
	await mockMe(page);
	const ended: string[] = [];
	page.on('request', (request) => {
		if (new URL(request.url()).pathname === '/oidc/logout') ended.push(request.url());
	});

	// Where a link in a mail, or an image on another site, would send a signed-in person.
	await page.goto('/auth/logout');

	await expect(page.getByRole('heading', { name: 'Page not found', exact: true })).toBeVisible();
	expect(ended).toEqual([]);
	// The issuer's session stands: the next page signs in from it without a login form.
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();
});

/** A refresh-token exchange at the mock issuer. */
const isRefresh = (request: { url(): string; postData(): string | null }): boolean =>
	request.url().endsWith('/oidc/token') &&
	(request.postData() ?? '').includes('grant_type=refresh_token');

test('a token about to expire is renewed without leaving the page, and what was typed stays', async ({
	context,
	page,
}) => {
	await signIn(context);
	// 62 s of life: the renewal is due a minute before the expiry, two seconds from now.
	await issuerBehaviour(context, { expiresIn: 62, renewedToken: 'e2e-token:admin:renewed' });
	await mockMe(page);
	const authorizations: string[] = [];
	page.on('request', (request) => {
		if (new URL(request.url()).pathname === '/oidc/auth') authorizations.push(request.url());
	});
	const renewal = page.waitForResponse((response) => isRefresh(response.request()));
	await page.goto('/catalog/tables');
	await page.getByRole('button', { name: 'Declare table' }).click();
	const typed = page.getByLabel('Table name');
	await typed.fill('half-typed');

	await renewal;
	// The session holds the renewed token once the exchange is answered: the page's next poll of lineage's
	// feed, which it makes without the person doing anything, carries it.
	const next = await page.waitForRequest((request) =>
		request.url().includes('/api/lineage/events'),
	);
	expect(next.headers()['authorization']).toBe('Bearer e2e-token:admin:renewed');
	await expect(typed).toHaveValue('half-typed');
	await expect(page.getByRole('alert').filter({ hasText: 'Your sign-in has ended' })).toHaveCount(
		0,
	);
	// One round trip to the issuer, the sign-in itself; the renewal made none.
	expect(authorizations).toHaveLength(1);
});

test('a door that answers 401 has the session renewed once, and is asked again', async ({
	context,
	page,
}) => {
	// The catalog knows no such token and answers 401, as it does a revoked or expired one.
	await signIn(context, { token: 'e2e-token:revoked' });
	await issuerBehaviour(context, { renewedToken: TOKEN.admin });
	const renewals: string[] = [];
	page.on('request', (request) => {
		if (isRefresh(request)) renewals.push(request.url());
	});

	await page.goto('/admin/dlq');

	// The identity read was refused, the session renewed, and the read answered: an estate admin's page.
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();
	await page.waitForLoadState('networkidle');
	expect(renewals).toHaveLength(1);
});

test('a door that refuses the renewed token too costs the issuer one exchange, not one for every read', async ({
	context,
	page,
}) => {
	await signIn(context, { token: 'e2e-token:revoked' });
	await issuerBehaviour(context, { renewedToken: 'e2e-token:revoked-again' });
	const renewals: string[] = [];
	page.on('request', (request) => {
		if (isRefresh(request)) renewals.push(request.url());
	});

	await page.goto('/catalog/tables');
	await page.waitForLoadState('networkidle');
	// More reads, each refused with the token the one renewal gave.
	// One more door refuses the token the session holds.
	await page.route('**/api/catalog/v1/namespace/**', (route) =>
		route.fulfill({ status: 401, json: { detail: 'not authenticated' } }),
	);
	const refusedAgain = page.waitForResponse('**/api/catalog/v1/namespace/**');
	await page
		.getByRole('navigation', { name: 'Pages' })
		.getByRole('link', { name: 'Namespaces' })
		.click();
	expect((await refusedAgain).status()).toBe(401);
	// The page says the read was refused only once the transport has answered it, so whatever the
	// transport asked of the issuer for that 401 has been asked by now.
	await expect(page.getByText('the warehouses read answered HTTP 401')).toBeVisible();

	expect(renewals).toHaveLength(1);
});

test('an ended session asks the issuer nothing more, whatever the page reads next', async ({
	context,
	page,
}) => {
	await signIn(context, { token: 'e2e-token:revoked' });
	await issuerBehaviour(context, { refuseRefresh: true });
	const renewals: string[] = [];
	page.on('request', (request) => {
		if (isRefresh(request)) renewals.push(request.url());
	});

	await page.goto('/catalog/tables');
	await expect(page.getByRole('alert').filter({ hasText: 'Your sign-in has ended' })).toBeVisible();
	// One more door refuses the token the session holds.
	await page.route('**/api/catalog/v1/namespace/**', (route) =>
		route.fulfill({ status: 401, json: { detail: 'not authenticated' } }),
	);
	const refusedAgain = page.waitForResponse('**/api/catalog/v1/namespace/**');
	await page
		.getByRole('navigation', { name: 'Pages' })
		.getByRole('link', { name: 'Namespaces' })
		.click();
	expect((await refusedAgain).status()).toBe(401);
	// The page says the read was refused only once the transport has answered it, so whatever the
	// transport asked of the issuer for that 401 has been asked by now.
	await expect(page.getByText('the warehouses read answered HTTP 401')).toBeVisible();

	expect(renewals).toHaveLength(1);
});

test('a session that cannot be renewed says so and keeps the page until the person signs in again', async ({
	context,
	page,
}) => {
	await signIn(context);
	await issuerBehaviour(context, { expiresIn: 3, refuseRefresh: true });
	await mockMe(page);
	await page.goto('/catalog/tables');
	await page.getByRole('button', { name: 'Declare table' }).click();
	const typed = page.getByLabel('Table name');
	await typed.fill('half-typed');

	const ended = page.getByRole('alert').filter({ hasText: 'Your sign-in has ended' });
	await expect(ended).toBeVisible();
	await expect(typed).toHaveValue('half-typed');
	await expect(page).toHaveURL(/\/catalog\/tables$/);

	// Signing in again is the person's choice; it comes back to the same page, renewed.
	await context.clearCookies({ name: 'mock_oidc_expires_in' });
	await context.clearCookies({ name: 'mock_oidc_refuse_refresh' });
	await ended.getByRole('button', { name: 'Sign in again' }).click();
	await expect(page).toHaveURL(/\/catalog\/tables$/);
	await expect(page.getByRole('heading', { name: 'Tables', exact: true })).toBeVisible();
	await expect(ended).toHaveCount(0);
});

test('a sign-in the issuer refuses says why and offers to try again, and exchanges nothing', async ({
	context,
	page,
}) => {
	await signIn(context);
	await issuerBehaviour(context, { error: 'access_denied' });
	const exchanges: string[] = [];
	page.on('request', (request) => {
		if (request.url().endsWith('/oidc/token')) exchanges.push(request.url());
	});

	await page.goto('/catalog/tables');

	await expect(
		page.getByText('Sign-in did not complete: the issuer answered access_denied'),
	).toBeVisible();
	await expect(page.getByRole('link', { name: 'Try again' })).toHaveAttribute(
		'href',
		'/auth/login',
	);
	expect(exchanges).toEqual([]);
});
