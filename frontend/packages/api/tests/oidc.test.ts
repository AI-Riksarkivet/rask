/**
 * Unit tests for the security-critical OIDC logic — runs under vitest (in the @rask/api package). Bun/Node ship
 * Web Crypto + a Jest-like runner). The network glue + SvelteKit routes are type-checked by svelte-check
 * and verified end-to-end against a live Dex; here we pin the pure pieces that are easy to get subtly wrong.
 */
import { describe, expect, test } from 'vitest';

import {
	type OidcConfig,
	type Session,
	base64url,
	base64urlDecode,
	buildAuthorizeUrl,
	decodeJwtClaims,
	decodeSession,
	deriveSessionKey,
	discover,
	encodeSession,
	exchangeCode,
	internalEndpoint,
	isExpired,
	pkceChallenge,
	randomToken,
	sessionFromTokens,
	tokenRequestBody,
} from '../src/oidc';

const CFG: OidcConfig = {
	issuer: 'https://dex.example.com',
	clientId: 'lance-web',
	clientSecret: null,
	redirectUri: 'https://app.example.com/auth/callback',
	scopes: 'openid profile email',
	sessionKey: null,
};

/** A JWT with the given claims payload (header/sig are throwaway — we never verify the sig in the BFF). */
function jwtWith(claims: Record<string, unknown>): string {
	return `header.${base64url(new TextEncoder().encode(JSON.stringify(claims)))}.sig`;
}

describe('base64url', () => {
	test('round-trips arbitrary UTF-8 bytes', () => {
		const text = 'naïve ✓ a/b+c=';
		expect(base64urlDecode(base64url(new TextEncoder().encode(text)))).toBe(text);
	});

	test('is url-safe and unpadded', () => {
		const encoded = base64url(new Uint8Array([251, 255, 191, 0]));
		expect(encoded).not.toMatch(/[+/=]/);
	});
});

describe('PKCE', () => {
	test('S256 challenge matches the RFC 7636 appendix-B test vector', async () => {
		// The canonical example from RFC 7636 §4.1/Appendix B — proves our S256 encoding is byte-correct.
		const verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk';
		expect(await pkceChallenge(verifier)).toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
	});

	test('randomToken is url-safe and high-entropy (distinct per call)', () => {
		const a = randomToken();
		const b = randomToken();
		expect(a).not.toBe(b);
		expect(a).not.toMatch(/[+/=]/);
	});
});

describe('authorize URL', () => {
	test('carries response_type=code, the client/redirect/scope, state, and S256 challenge', () => {
		const url = new URL(buildAuthorizeUrl(CFG, `${CFG.issuer}/auth`, 'st4te', 'ch4llenge'));
		const p = url.searchParams;
		expect(p.get('response_type')).toBe('code');
		expect(p.get('client_id')).toBe('lance-web');
		expect(p.get('redirect_uri')).toBe(CFG.redirectUri);
		expect(p.get('scope')).toBe('openid profile email');
		expect(p.get('state')).toBe('st4te');
		expect(p.get('code_challenge')).toBe('ch4llenge');
		expect(p.get('code_challenge_method')).toBe('S256');
	});
});

describe('token request body', () => {
	test('is an authorization_code grant carrying the PKCE verifier (no secret for a public client)', () => {
		const body = tokenRequestBody(CFG, 'the-code', 'the-verifier');
		expect(body.get('grant_type')).toBe('authorization_code');
		expect(body.get('code')).toBe('the-code');
		expect(body.get('code_verifier')).toBe('the-verifier');
		expect(body.get('client_id')).toBe('lance-web');
		expect(body.get('client_secret')).toBeNull();
	});

	test('includes the client_secret for a confidential client', () => {
		const body = tokenRequestBody({ ...CFG, clientSecret: 's3cret' }, 'c', 'v');
		expect(body.get('client_secret')).toBe('s3cret');
	});
});

describe('JWT + session', () => {
	test('decodeJwtClaims reads the (unverified) payload', () => {
		expect(decodeJwtClaims(jwtWith({ sub: 'u1', name: 'Dee' })).name).toBe('Dee');
	});

	test('decodeJwtClaims rejects a malformed token', () => {
		expect(() => decodeJwtClaims('not-a-jwt')).toThrow();
	});

	test('sessionFromTokens lifts sub/name/email/exp + binds the access token', () => {
		const s = sessionFromTokens(
			jwtWith({ sub: 'u1', name: 'Dee', email: 'dee@x.io', exp: 4102444800 }),
			'AT-123',
		);
		expect(s).toEqual({
			sub: 'u1',
			name: 'Dee',
			email: 'dee@x.io',
			accessToken: 'AT-123',
			expiresAt: 4102444800,
		});
	});

	test('sessionFromTokens falls back through preferred_username → email → sub for the display name', () => {
		expect(sessionFromTokens(jwtWith({ sub: 'u2', preferred_username: 'pu' }), 'AT').name).toBe(
			'pu',
		);
		expect(sessionFromTokens(jwtWith({ sub: 'u3' }), 'AT').name).toBe('u3');
	});

	test('encode/decode session round-trips (dev base64); a tampered/empty cookie decodes to null', () => {
		const s: Session = { sub: 'u1', name: 'Dee', email: null, accessToken: 'AT', expiresAt: 0 };
		expect(decodeSession(encodeSession(s))).toEqual(s);
		expect(decodeSession('@@not-base64@@')).toBeNull();
		expect(decodeSession(base64url(new TextEncoder().encode('{}')))).toBeNull(); // no sub/token
	});

	test('a session key SEALS the cookie (AES-256-GCM): round-trips, tamper → null, wrong key → null', () => {
		// The token is long and distinctive ON PURPOSE. With the two-character 'AT' the other tests use,
		// `not.toContain` was a coin flip rather than an assertion: the sealed cookie is `v1.` plus ~130
		// base64url characters of random ciphertext, and a 2-char needle turns up in that haystack about
		// 3% of the time. Measured, not estimated — 616 of 20000 seals contained 'AT', which is one CI run
		// in 32 going red on a SECURITY assertion for no reason, and the natural response to that is to
		// re-run the job rather than read it. The same 20000 seals contained this token 0 times.
		const token = 'access-token-must-not-be-readable';
		const s: Session = { sub: 'u1', name: 'Dee', email: null, accessToken: token, expiresAt: 0 };
		const key = deriveSessionKey('server-secret');
		const sealed = encodeSession(s, key);
		expect(sealed.startsWith('v1.')).toBe(true); // sealed form, not raw base64
		expect(sealed).not.toContain(token); // the access token is NOT readable in the cookie
		expect(decodeSession(sealed, key)).toEqual(s); // round-trip with the right key
		expect(decodeSession(sealed, deriveSessionKey('other-secret'))).toBeNull(); // wrong key → GCM fails
		// Tamper with the tail, and make sure it is ACTUALLY a change: substituting a fixed 'AAAA' is a
		// no-op on the 1-in-16-million cookie that already ends that way, and a no-op tamper would pass
		// this assertion for the wrong reason.
		const tampered = sealed.slice(0, -4) + (sealed.endsWith('AAAA') ? 'BBBB' : 'AAAA');
		expect(tampered).not.toBe(sealed);
		expect(decodeSession(tampered, key)).toBeNull(); // tampered ciphertext → null
	});

	test('with a key, an UNSEALED (forged base64) cookie is rejected — no impersonation', () => {
		const s: Session = {
			sub: 'attacker',
			name: 'x',
			email: null,
			accessToken: 'forged',
			expiresAt: 0,
		};
		const key = deriveSessionKey('server-secret');
		const forged = encodeSession(s, null); // plain base64, no key
		expect(decodeSession(forged, key)).toBeNull(); // prod (key set) must not accept it
	});

	test('a sealed cookie is unreadable without the key (fail closed)', () => {
		const s: Session = { sub: 'u1', name: 'Dee', email: null, accessToken: 'AT', expiresAt: 0 };
		expect(decodeSession(encodeSession(s, deriveSessionKey('k')), null)).toBeNull();
	});

	test('isExpired honors exp; exp=0 (no claim) is treated as live', () => {
		expect(
			isExpired({ sub: 'u', name: 'n', email: null, accessToken: 'a', expiresAt: 100 }, 200),
		).toBe(true);
		expect(
			isExpired({ sub: 'u', name: 'n', email: null, accessToken: 'a', expiresAt: 300 }, 200),
		).toBe(false);
		expect(
			isExpired({ sub: 'u', name: 'n', email: null, accessToken: 'a', expiresAt: 0 }, 200),
		).toBe(false);
	});
});

describe('exchangeCode', () => {
	test('POSTs the code-grant body and lifts the token response into a session', async () => {
		let captured: { url: string; body: string } | null = null;
		const fakeFetch = (async (url: string | URL | Request, init?: RequestInit) => {
			captured = { url: String(url), body: String(init?.body) };
			return new Response(
				JSON.stringify({ access_token: 'AT-xyz', id_token: jwtWith({ sub: 'u9', name: 'Nine' }) }),
				{ status: 200, headers: { 'content-type': 'application/json' } },
			);
		}) as typeof fetch;

		const session = await exchangeCode(CFG, `${CFG.issuer}/token`, 'code-1', 'ver-1', fakeFetch);
		expect(session.sub).toBe('u9');
		expect(session.accessToken).toBe('AT-xyz');
		expect(captured!.url).toBe(`${CFG.issuer}/token`);
		expect(captured!.body).toContain('grant_type=authorization_code');
		expect(captured!.body).toContain('code_verifier=ver-1');
	});

	test('throws on a non-2xx token response (so the callback fails closed)', async () => {
		const fakeFetch = (async () =>
			new Response('nope', { status: 401 })) as unknown as typeof fetch;
		await expect(exchangeCode(CFG, `${CFG.issuer}/token`, 'c', 'v', fakeFetch)).rejects.toThrow();
	});
});

describe('split-horizon internal issuer', () => {
	const SPLIT: OidcConfig = { ...CFG, internalIssuer: 'http://lance-dex:5556/dex' };
	const jsonFetch = (capture: (url: string) => void, body: unknown) =>
		(async (url: string | URL | Request) => {
			capture(String(url));
			return new Response(JSON.stringify(body), {
				status: 200,
				headers: { 'content-type': 'application/json' },
			});
		}) as typeof fetch;

	test('internalEndpoint rewrites a public-issuer URL onto the internal base', () => {
		expect(internalEndpoint(SPLIT, `${CFG.issuer}/token`)).toBe('http://lance-dex:5556/dex/token');
	});

	test('internalEndpoint is a no-op without an internal issuer, or for a foreign URL', () => {
		expect(internalEndpoint(CFG, `${CFG.issuer}/token`)).toBe(`${CFG.issuer}/token`);
		expect(internalEndpoint(SPLIT, 'https://elsewhere.example.com/token')).toBe(
			'https://elsewhere.example.com/token',
		);
	});

	test('discover fetches from the internal issuer, returning the endpoints as rendered (public)', async () => {
		let url = '';
		const doc = {
			authorization_endpoint: `${CFG.issuer}/auth`,
			token_endpoint: `${CFG.issuer}/token`,
		};
		const got = await discover(
			SPLIT,
			jsonFetch((u) => (url = u), doc),
		);
		expect(url).toBe('http://lance-dex:5556/dex/.well-known/openid-configuration');
		// The authorize URL the BROWSER is redirected to must stay the public one — no rewrite here.
		expect(got.authorization_endpoint).toBe(`${CFG.issuer}/auth`);
	});

	test('discover stays on the public issuer when no internal one is configured', async () => {
		let url = '';
		await discover(
			CFG,
			jsonFetch((u) => (url = u), { authorization_endpoint: 'a', token_endpoint: 't' }),
		);
		expect(url).toBe(`${CFG.issuer}/.well-known/openid-configuration`);
	});

	test('exchangeCode POSTs to the REWRITTEN internal token endpoint (the discovered one is public)', async () => {
		let url = '';
		const session = await exchangeCode(
			SPLIT,
			`${CFG.issuer}/token`, // as discovered: Dex renders its endpoints from the PUBLIC issuer
			'code-1',
			'ver-1',
			jsonFetch((u) => (url = u), {
				access_token: 'AT',
				id_token: jwtWith({ sub: 'u1' }),
			}),
		);
		expect(url).toBe('http://lance-dex:5556/dex/token');
		expect(session.sub).toBe('u1');
	});
});
