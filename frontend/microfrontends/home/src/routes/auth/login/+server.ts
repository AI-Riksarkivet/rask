/**
 * Start the OIDC login (home zone owns /auth/* — it's the catch-all at the origin root, so the cookies it
 * sets are visible to every path-routed zone). Mint a PKCE verifier + CSRF state, stash them + the
 * post-login return path in short-lived httpOnly cookies, and redirect the browser to Dex's authorize
 * endpoint. No-op redirect home when auth is unconfigured (the stack runs auth-off).
 */
import { env } from '$env/dynamic/private';
import { redirect, type RequestHandler } from '@sveltejs/kit';
import {
	RETURN_COOKIE,
	STATE_COOKIE,
	VERIFIER_COOKIE,
	buildAuthorizeUrl,
	discover,
	pkceChallenge,
	randomToken,
} from '@rask/api/oidc';
import { makeOidcConfig } from '@rask/api/bff';

const TEMP_COOKIE_MAX_AGE = 600; // 10 min — long enough to complete the round-trip to Dex.

/** Only same-origin absolute paths (`/…`, not `//host` protocol-relative) are allowed — an open-redirect guard. */
function safeReturn(raw: string | null): string {
	return raw && raw.startsWith('/') && !raw.startsWith('//') ? raw : '/';
}

export const GET: RequestHandler = async ({ url, cookies, fetch }) => {
	const cfg = makeOidcConfig(env);
	if (!cfg) redirect(302, '/');

	const verifier = randomToken();
	const state = randomToken(16);
	const challenge = await pkceChallenge(verifier);
	const opts = { path: '/', httpOnly: true, sameSite: 'lax', maxAge: TEMP_COOKIE_MAX_AGE } as const;
	cookies.set(VERIFIER_COOKIE, verifier, opts);
	cookies.set(STATE_COOKIE, state, opts);
	cookies.set(RETURN_COOKIE, safeReturn(url.searchParams.get('redirect')), opts);

	const { authorization_endpoint } = await discover(cfg, fetch);
	redirect(302, buildAuthorizeUrl(cfg, authorization_endpoint, state, challenge));
};
