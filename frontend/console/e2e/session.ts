import type { BrowserContext } from '@playwright/test';
import { AUTH_ON } from './ports';

// Sign the e2e browser context in. The estate pages' preview runs auth-ON (it serves a config naming
// the mock issuer), because the whole surface keys off `/management/v1/me`: the estate-admin create door,
// the membership gallery and one project's overview all read an identity, and an identity needs a
// bearer. `signIn` tells the mock issuer who to sign in as; the console's real PKCE round trip then
// hands that bearer to every API call.
//
// The mock catalog derives NO identity from the token: each spec SEEDS the `me` it is testing under its
// bearer. The prefixes below are naming, not behaviour — they keep a ledger dump readable.
export const TOKEN = {
	admin: 'e2e-token:admin',
	member: 'e2e-token:member',
} as const;

/** The frozen `/management/v1/me` shape of an ESTATE ADMIN — the only identity the create affordance renders for.
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
 * The bearer is what the catalog sees — every read and write this surface makes carries it — so it is
 * also the key the mock catalog files this test's seeds and ledger under. Specs mint one per test
 * (`${TOKEN.admin}:${testInfo.testId}`) so the fullyParallel suite shares no mutable mock state.
 */
export async function signIn(
	context: BrowserContext,
	{ token = TOKEN.admin, origin = AUTH_ON }: { token?: string; origin?: string } = {},
) {
	const identity = { token, sub: 'user:alice', name: 'Alice', email: 'alice@example.com' };
	await context.addCookies([
		{
			name: 'mock_oidc_identity',
			value: encodeURIComponent(JSON.stringify(identity)),
			url: origin,
		},
	]);
}
