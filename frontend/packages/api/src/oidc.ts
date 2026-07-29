/**
 * Pure + network OIDC helpers, with NO SvelteKit (`$env`) import, so they run under `bun test`.
 * `oidc.ts` re-exports everything here and adds only `oidcConfig()` (which reads `$env`).
 *
 * The pure pieces (PKCE, state, authorize-URL, token-request body, JWT-claims decode, session codec)
 * are unit-tested in `oidc-core.test.ts`; `discover` / `exchangeCode` take an injectable `fetch` so the
 * exchange is testable against a fake token endpoint too. `node:crypto` is a builtin (server-only file, and
 * bun supports it), so the sealed-session codec is unit-testable too.
 */
import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';

export const SESSION_COOKIE = 'lance_session';
export const VERIFIER_COOKIE = 'oidc_verifier';
export const STATE_COOKIE = 'oidc_state';
export const RETURN_COOKIE = 'oidc_return';
const SEALED_PREFIX = 'v1.'; // marks an AES-256-GCM sealed cookie vs the legacy/dev base64 form

export type OidcConfig = {
	issuer: string;
	clientId: string;
	clientSecret: string | null;
	redirectUri: string;
	scopes: string;
	/** 32-byte AES key derived from `SESSION_SECRET`, or `null` in dev — the cookie is then base64 (unsealed). */
	sessionKey: Buffer | null;
	/** Split-horizon issuer for SERVER-side fetches (discovery, token POST) — e.g. the in-cluster Dex
	 * Service URL when the public `issuer` is only browser-reachable. The BROWSER-facing authorize URL and
	 * the token's `iss` keep the public `issuer`. `null`/unset → all fetches use the public issuer. */
	internalIssuer?: string | null;
};

export type Session = {
	sub: string;
	name: string;
	email: string | null;
	accessToken: string;
	expiresAt: number; // epoch seconds
};

/** RFC 4648 §5 base64url (no padding) of raw bytes. */
export function base64url(bytes: Uint8Array): string {
	let bin = '';
	for (const b of bytes) bin += String.fromCharCode(b);
	return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Decode a base64url string back to UTF-8 text (restores padding first). */
export function base64urlDecode(s: string): string {
	const b64 = s.replace(/-/g, '+').replace(/_/g, '/');
	const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
	const bin = atob(padded);
	return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

/** A cryptographically-random base64url token (the PKCE verifier / the CSRF state). */
export function randomToken(bytes = 32): string {
	return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** The PKCE S256 challenge for a verifier: base64url(SHA-256(verifier)). */
export async function pkceChallenge(verifier: string): Promise<string> {
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
	return base64url(new Uint8Array(digest));
}

/** The Dex authorize URL the browser is redirected to (PKCE S256, CSRF state). */
export function buildAuthorizeUrl(
	cfg: OidcConfig,
	authorizationEndpoint: string,
	state: string,
	challenge: string,
): string {
	const url = new URL(authorizationEndpoint);
	url.searchParams.set('response_type', 'code');
	url.searchParams.set('client_id', cfg.clientId);
	url.searchParams.set('redirect_uri', cfg.redirectUri);
	url.searchParams.set('scope', cfg.scopes);
	url.searchParams.set('state', state);
	url.searchParams.set('code_challenge', challenge);
	url.searchParams.set('code_challenge_method', 'S256');
	return url.toString();
}

/** The url-encoded body for the authorization-code token exchange. */
export function tokenRequestBody(cfg: OidcConfig, code: string, verifier: string): URLSearchParams {
	const body = new URLSearchParams({
		grant_type: 'authorization_code',
		code,
		redirect_uri: cfg.redirectUri,
		client_id: cfg.clientId,
		code_verifier: verifier,
	});
	if (cfg.clientSecret) body.set('client_secret', cfg.clientSecret);
	return body;
}

/** Decode (NOT verify) a JWT's claims — trusted because it came straight from the token endpoint over TLS. */
export function decodeJwtClaims(jwt: string): Record<string, unknown> {
	const payload = jwt.split('.')[1];
	if (!payload) throw new Error('malformed JWT');
	return JSON.parse(base64urlDecode(payload));
}

/** Build the session from a token response's id_token + access_token. */
export function sessionFromTokens(idToken: string, accessToken: string): Session {
	const claims = decodeJwtClaims(idToken);
	const exp = typeof claims.exp === 'number' ? claims.exp : 0;
	return {
		sub: String(claims.sub ?? ''),
		name: String(claims.name ?? claims.preferred_username ?? claims.email ?? claims.sub ?? 'user'),
		email: typeof claims.email === 'string' ? claims.email : null,
		accessToken,
		expiresAt: exp,
	};
}

/** A 32-byte AES-256 key from the server `SESSION_SECRET` (SHA-256, so any secret length works). */
export function deriveSessionKey(secret: string): Buffer {
	return createHash('sha256').update(secret).digest();
}

/** Serialize the session into the httpOnly cookie. With a key: AES-256-GCM sealed (confidential + tamper-
 * evident) as ``v1.base64url(iv[12]||tag[16]||ciphertext)``. Without a key (dev): base64url JSON, unsealed —
 * documented demo-grade. */
export function encodeSession(session: Session, key: Buffer | null = null): string {
	const plain = new TextEncoder().encode(JSON.stringify(session));
	if (!key) return base64url(plain);
	const iv = randomBytes(12);
	const cipher = createCipheriv('aes-256-gcm', key, iv);
	const ct = Buffer.concat([cipher.update(Buffer.from(plain)), cipher.final()]);
	return SEALED_PREFIX + base64url(Buffer.concat([iv, cipher.getAuthTag(), ct]));
}

/** Parse the session cookie. When a key is configured (production) ONLY a valid sealed cookie is accepted —
 * an unsealed or tampered cookie returns ``null``, so a forged base64 cookie can't impersonate a user. In
 * dev (no key) the legacy base64 form is read. Returns ``null`` on any malformed/invalid input. */
export function decodeSession(raw: string, key: Buffer | null = null): Session | null {
	try {
		let json: string;
		if (key) {
			if (!raw.startsWith(SEALED_PREFIX)) return null; // prod: reject anything not sealed by us
			const buf = Buffer.from(
				raw.slice(SEALED_PREFIX.length).replace(/-/g, '+').replace(/_/g, '/'),
				'base64',
			);
			const iv = buf.subarray(0, 12);
			const tag = buf.subarray(12, 28);
			const ct = buf.subarray(28);
			const decipher = createDecipheriv('aes-256-gcm', key, iv);
			decipher.setAuthTag(tag);
			json = Buffer.concat([decipher.update(ct), decipher.final()]).toString('utf8');
		} else {
			if (raw.startsWith(SEALED_PREFIX)) return null; // sealed cookie but no key → can't read
			json = base64urlDecode(raw);
		}
		const obj = JSON.parse(json) as Session;
		if (!obj.sub || !obj.accessToken) return null;
		return obj;
	} catch {
		return null;
	}
}

/** Has the session's access token expired? (0 = no expiry claim → treat as live for the demo.) */
export function isExpired(session: Session, nowSeconds: number): boolean {
	return session.expiresAt > 0 && session.expiresAt <= nowSeconds;
}

type Discovery = {
	authorization_endpoint: string;
	token_endpoint: string;
	end_session_endpoint?: string;
};

/** Rewrite a discovered endpoint URL onto the internal issuer base for SERVER-side calls. The discovery
 * doc's endpoints are PUBLIC URLs (Dex renders them from its public issuer), which pods may not be able to
 * reach (split-horizon: the public host resolves for browsers, not in-cluster) — so the server-side
 * exchange swaps the public-issuer prefix for the internal one. A URL not under the public issuer, or no
 * internal issuer configured, passes through unchanged. */
export function internalEndpoint(cfg: OidcConfig, url: string): string {
	if (!cfg.internalIssuer) return url;
	return url.startsWith(cfg.issuer) ? cfg.internalIssuer + url.slice(cfg.issuer.length) : url;
}

/** Fetch the OIDC discovery document (`/.well-known/openid-configuration`) — a SERVER-side fetch, so it
 * goes to the internal issuer when one is configured. The endpoints it returns stay as rendered (public
 * URLs): the authorize URL the browser is redirected to must be the public one; server-side consumers
 * (the token exchange) rewrite via `internalEndpoint`. */
export async function discover(cfg: OidcConfig, fetchFn: typeof fetch = fetch): Promise<Discovery> {
	const base = cfg.internalIssuer ?? cfg.issuer;
	const res = await fetchFn(`${base}/.well-known/openid-configuration`);
	if (!res.ok) throw new Error(`OIDC discovery failed: ${res.status}`);
	return (await res.json()) as Discovery;
}

/** Exchange an authorization code (+ PKCE verifier) for tokens, returning the session. A SERVER-side
 * POST: the discovered (public) token endpoint is rewritten onto the internal issuer when configured —
 * see `internalEndpoint` for why. The `iss` inside the returned id_token stays the public issuer (Dex
 * signs with its public identity regardless of which horizon served the request). */
export async function exchangeCode(
	cfg: OidcConfig,
	tokenEndpoint: string,
	code: string,
	verifier: string,
	fetchFn: typeof fetch = fetch,
): Promise<Session> {
	const res = await fetchFn(internalEndpoint(cfg, tokenEndpoint), {
		method: 'POST',
		headers: { 'content-type': 'application/x-www-form-urlencoded' },
		body: tokenRequestBody(cfg, code, verifier),
	});
	if (!res.ok) throw new Error(`token exchange failed: ${res.status}`);
	const tokens = (await res.json()) as { access_token: string; id_token: string };
	return sessionFromTokens(tokens.id_token, tokens.access_token);
}
