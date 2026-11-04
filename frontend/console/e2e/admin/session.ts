import type { BrowserContext, Page } from '@playwright/test';
import { AUTH_ON } from './ports';

// Sign the e2e browser context in. The lakehouse pages' preview runs auth-ON (it serves a config
// naming the mock issuer), so a signed-out visit goes to the issuer; the panel specs therefore run
// SIGNED-IN, through the console's real PKCE round trip against the mock issuer.
/** The bearer each identity signs in with. The mock catalog resolves `/management/v1/me` from it, so
 *  the identity is per browser CONTEXT: the suite is fullyParallel and a shared "current identity" on
 *  the mock would race every spec's door. */
export const TOKEN = {
	admin: 'e2e-token:admin',
	member: 'e2e-token:member',
	/** Resolves to a 502 — the catalog-outage case, which must fail CLOSED. */
	down: 'e2e-token:down',
} as const;

const SESSION = {
	sub: 'user:e2e',
	name: 'E2E Admin',
	email: 'e2e@example.com',
};

/** The display name the shared nav-user renders for the minted session (auth.spec.ts asserts it). */
export const SESSION_NAME = SESSION.name;

// The frozen /management/v1/me shapes the layout door + navbar read.
export const ME_ADMIN = {
	sub: 'user:e2e',
	name: 'E2E Admin',
	email: 'e2e@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

/** A bob-shaped identity: verified, but WITHOUT the estate-admin privilege — the admin area's
 *  layout-level door must refuse and the navbar must hide Operations. */
export const ME_MEMBER = {
	sub: 'user:bob',
	name: 'Bob',
	email: 'bob@example.com',
	estate_admin: false,
	projects: [{ project: 'acme', role: 'member' }],
};

/** Answer `/management/v1/me` in the browser (default: an estate admin, so panels render). */
export async function mockMe(page: Page, me: unknown = ME_ADMIN) {
	await page.route('**/api/catalog/management/v1/me', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(me) }),
	);
}

/**
 * Sign the e2e browser context in AS a given identity: the mock issuer signs the console in with
 * `token`, which every API call then carries. `mockMe` answers `/management/v1/me` in the browser
 * instead of the mock catalog; a spec testing the door sets both, and they must agree.
 */
export async function signIn(
	context: BrowserContext,
	{ token = TOKEN.admin, origin = AUTH_ON }: { token?: string; origin?: string } = {},
) {
	const identity = { token, sub: SESSION.sub, name: SESSION.name, email: SESSION.email };
	await context.addCookies([
		{
			name: 'mock_oidc_identity',
			value: encodeURIComponent(JSON.stringify(identity)),
			url: origin,
		},
	]);
}
