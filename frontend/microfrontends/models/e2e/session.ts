import type { BrowserContext } from '@playwright/test';
import { APP } from './ports';

/**
 * Sign the e2e browser context in.
 *
 * This zone's e2e dev server runs AUTH-ON (OIDC env in playwright.config.ts) for two reasons, and only
 * the second one is about auth:
 *
 *  1. `models.remote.ts` and `experiments.remote.ts` both read `locals.session?.accessToken` and refuse
 *     without it — the governed behaviour is the behaviour worth testing, and the auth-off variant would
 *     exercise a code path the estate does not deploy.
 *  2. The BEARER is how a test isolates its world. The suite is `fullyParallel` and the mock upstream keys
 *     every seed and every call-log entry by bearer, so each test signs in with its own token and cannot
 *     see (or be raced by) another's fixtures.
 *
 * The login-first gate redirects a signed-out page navigation to `/auth/login` — a HOME-zone route, absent
 * from this isolated server — so every spec must sign in before its first `goto`, warmup included.
 *
 * The server has no SESSION_SECRET, so the BFF accepts the documented dev-grade UNSEALED base64url session
 * cookie, which is what this mints.
 */

/** The bearer this zone's specs sign in with. Specs append a per-test suffix (`e2e-token:admin:<testId>`)
 *  so each test owns its bucket on the mock. */
export const TOKEN = { admin: 'e2e-token:admin' } as const;

/**
 * The frozen `/v1/me` identity, seeded on the mock catalog by the specs that care.
 *
 * The zone layout contract resolves `me` SERVER-SIDE now (`makeZoneLayoutLoad` → `fetchMe` against
 * CATALOG_API, #107) rather than the browser fetching `/capi/v1/me` — so `page.route` cannot stand in for
 * it and the identity has to be seeded like every other server-side read. `fetchMe` degrades to `null` on
 * ANY failure, so a spec that seeds nothing gets the UNRESOLVED-identity navbar, which is a real state
 * worth asserting rather than an accident of the harness.
 */
export const ME_ADMIN = {
	sub: 'user:e2e',
	name: 'E2E Admin',
	email: 'e2e@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

const SESSION = {
	sub: 'user:e2e',
	name: 'E2E Admin',
	email: 'e2e@example.com',
	expiresAt: 0, // no expiry claim → treated live
};

export async function signIn(
	context: BrowserContext,
	{ token = TOKEN.admin, origin = APP }: { token?: string; origin?: string } = {},
) {
	const value = Buffer.from(JSON.stringify({ ...SESSION, accessToken: token }), 'utf8')
		.toString('base64')
		.replace(/\+/g, '-')
		.replace(/\//g, '_')
		.replace(/=+$/, '');
	await context.addCookies([{ name: 'lance_session', value, url: origin, httpOnly: true }]);
}
