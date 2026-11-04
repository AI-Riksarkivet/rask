// A stand-in OIDC issuer for the auth-ON origins, served same-origin under `/oidc` (the e2e edge passes
// the browser's host in `x-forwarded-host`, so the discovery document names that origin). The console's real
// sign-in path runs against it end to end — discovery, the authorization-code redirect with PKCE, the
// token exchange — so no spec needs an auth bypass in the app.
//
// WHO SIGNS IN is the spec's choice: `signIn()` (session.ts) sets the `mock_oidc_identity` cookie, and
// the authorize step carries that identity inside the code it hands back, so the exchange is stateless
// and parallel specs never share an identity. With no cookie the issuer shows a login page and stops,
// which is what a signed-out visit looks like.

import { MOCK_OIDC_PORT } from './ports';

/** What a spec signs in as. `token` is the bearer every API call then carries. */
export interface MockIdentity {
	token: string;
	sub: string;
	name: string;
	email: string;
}

const COOKIE = 'mock_oidc_identity';

const b64url = (text: string): string => Buffer.from(text, 'utf8').toString('base64url');
const unb64url = (text: string): string => Buffer.from(text, 'base64url').toString('utf8');

function cookieOf(req: Request, name: string): string | null {
	for (const part of (req.headers.get('cookie') ?? '').split(';')) {
		const [key, ...value] = part.trim().split('=');
		if (key === name) return decodeURIComponent(value.join('='));
	}
	return null;
}

const json = (body: unknown, status = 200): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_OIDC_PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const issuer = `http://${req.headers.get('x-forwarded-host') ?? req.headers.get('host')}/oidc`;

		if (url.pathname === '/.well-known/openid-configuration') {
			return json({
				issuer,
				authorization_endpoint: `${issuer}/auth`,
				token_endpoint: `${issuer}/token`,
				jwks_uri: `${issuer}/keys`,
				response_types_supported: ['code'],
				subject_types_supported: ['public'],
				id_token_signing_alg_values_supported: ['none'],
				code_challenge_methods_supported: ['S256'],
			});
		}
		if (url.pathname === '/keys') return json({ keys: [] });

		if (url.pathname === '/auth') {
			const identity = cookieOf(req, COOKIE);
			if (!identity) {
				return new Response('<!doctype html><title>Mock IdP</title><h1>Mock IdP login</h1>', {
					headers: { 'content-type': 'text/html' },
				});
			}
			const back = new URL(url.searchParams.get('redirect_uri') ?? '');
			back.searchParams.set('code', b64url(identity));
			back.searchParams.set('state', url.searchParams.get('state') ?? '');
			return new Response(null, { status: 302, headers: { location: back.href } });
		}

		if (url.pathname === '/token' && req.method === 'POST') {
			const form = new URLSearchParams(await req.text());
			const who = JSON.parse(unb64url(form.get('code') ?? '')) as MockIdentity;
			const now = Math.floor(Date.now() / 1000);
			const claims = {
				iss: issuer,
				aud: form.get('client_id'),
				sub: who.sub,
				name: who.name,
				email: who.email,
				iat: now,
				exp: now + 3600,
			};
			const idToken = `${b64url('{"alg":"none","typ":"JWT"}')}.${b64url(JSON.stringify(claims))}.`;
			return json({
				access_token: who.token,
				id_token: idToken,
				token_type: 'Bearer',
				expires_in: 3600,
			});
		}
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});

console.log(`mock OIDC issuer on :${MOCK_OIDC_PORT}`);
