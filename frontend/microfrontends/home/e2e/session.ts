import type { BrowserContext } from '@playwright/test';
import { AUTH_ON } from './ports';

// Sign the e2e browser context in. The projects dev server runs auth-ON (OIDC env in
// playwright.config.ts) because the whole surface keys off `/v1/me`: the estate-admin create door, the
// membership gallery and one project's overview all read an identity, and an identity needs a bearer.
// The server has no SESSION_SECRET, so the BFF accepts the documented dev-grade UNSEALED base64url
// session cookie, which we mint here.
//
// UNLIKE the lakehouse's session helper, this mock derives NO identity from the token: home's layout
// calls `fetchMe` against CATALOG_API directly (no `/capi/v1/me` browser hop to `page.route`), so the
// identity is SEEDED per bearer on the mock catalog and each spec states the `me` it is testing. The
// prefixes below are therefore naming, not behaviour — they keep a ledger dump readable.
export const TOKEN = {
	admin: 'e2e-token:admin',
	member: 'e2e-token:member',
} as const;

const SESSION = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	expiresAt: 0, // no expiry claim → treated live
};

/** The frozen `/v1/me` shape of an ESTATE ADMIN — the only identity the create affordance renders for.
 *  `projects: []` on purpose: an estate admin's gallery comes from the estate listing, not memberships,
 *  so a spec that wants memberships states them itself (`{...ME_ADMIN, projects: [...]}`). */
export const ME_ADMIN = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [] as Array<{ project: string; role: 'admin' | 'member' }>,
};

/** A bob-shaped identity: verified, but WITHOUT the estate-admin privilege. Its gallery is exactly its
 *  memberships (the estate listing is never even attempted) and the create affordance must be absent. */
export const ME_MEMBER = {
	sub: 'user:bob',
	name: 'Bob',
	email: 'bob@example.com',
	estate_admin: false,
	projects: [{ project: 'acme', role: 'member' }],
};

/**
 * Sign the e2e browser context in with a given bearer.
 *
 * The bearer is what the SERVER sees — every read and write this surface makes runs on the zone server
 * and forwards it, so it is also the key the mock catalog files this test's seeds and ledger under.
 * Specs mint one per test (`${TOKEN.admin}:${testInfo.testId}`) so the fullyParallel suite shares no
 * mutable mock state.
 */
export async function signIn(
	context: BrowserContext,
	{ token = TOKEN.admin, origin = AUTH_ON }: { token?: string; origin?: string } = {},
) {
	const value = Buffer.from(JSON.stringify({ ...SESSION, accessToken: token }), 'utf8')
		.toString('base64')
		.replace(/\+/g, '-')
		.replace(/\//g, '_')
		.replace(/=+$/, '');
	await context.addCookies([{ name: 'lance_session', value, url: origin, httpOnly: true }]);
}
