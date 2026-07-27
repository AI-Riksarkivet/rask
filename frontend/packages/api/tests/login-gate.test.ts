/**
 * Unit tests for the login-first gate in `makeSessionHandle` — the ONE predicate all five shell zones
 * inherit. Pins: an unauthenticated browser page navigation redirects to /auth/login?redirect=<path>
 * ONLY when OIDC is configured; API/fetch clients, /auth/* itself, assets, non-GET, and data requests
 * pass through; a valid session passes through.
 */
import type { Handle } from '@sveltejs/kit';
import { describe, expect, test } from 'vitest';
import { SESSION_COOKIE, encodeSession, type Session } from '../src/oidc';
import { isGatedPageRequest, makeOidcConfig, makeSessionHandle } from '../src/bff';

const CFG = makeOidcConfig({
	OIDC_ISSUER: 'https://dex.example.com/dex',
	OIDC_CLIENT_ID: 'lance-web',
	OIDC_REDIRECT_URI: 'https://app.example.com/auth/callback',
});

const SESSION: Session = {
	sub: 'user:alice',
	name: 'Alice',
	email: null,
	accessToken: 'AT',
	expiresAt: 0,
};

/** A minimal RequestEvent double: enough surface for makeSessionHandle (cookies/url/request/locals). */
function fakeEvent(
	path: string,
	opts: { method?: string; accept?: string; cookie?: string; isDataRequest?: boolean } = {},
) {
	const url = new URL(`https://app.example.com${path}`);
	return {
		url,
		isDataRequest: opts.isDataRequest ?? false,
		request: new Request(url, {
			method: opts.method ?? 'GET',
			headers: { accept: opts.accept ?? 'text/html,application/xhtml+xml' },
		}),
		cookies: {
			get: (name: string) => (name === SESSION_COOKIE ? opts.cookie : undefined),
			delete: () => {},
		},
		locals: {},
	};
}

const PAGE = new Response('page', { status: 200 });
type KitEvent = Parameters<Handle>[0]['event'];
const run = (cfg: ReturnType<typeof makeOidcConfig>, event: ReturnType<typeof fakeEvent>) =>
	makeSessionHandle(cfg)({ event: event as unknown as KitEvent, resolve: async () => PAGE });

describe('login-first gate', () => {
	test('a signed-out browser page navigation redirects to /auth/login?redirect=<original path>', async () => {
		const res = await run(CFG, fakeEvent('/data/tables?ns=bronze'));
		expect(res.status).toBe(302);
		expect(res.headers.get('location')).toBe(
			`/auth/login?redirect=${encodeURIComponent('/data/tables?ns=bronze')}`,
		);
	});

	test('with NO OIDC config (dev / hermetic e2e) the same request resolves — behavior unchanged', async () => {
		const res = await run(null, fakeEvent('/data/tables'));
		expect(res.status).toBe(200);
	});

	test('a valid session cookie passes through', async () => {
		const res = await run(CFG, fakeEvent('/data/tables', { cookie: encodeSession(SESSION) }));
		expect(res.status).toBe(200);
	});

	test('API fetch clients keep their 401 JSON contract: no text/html Accept → no redirect', async () => {
		const res = await run(CFG, fakeEvent('/admin/api/audit', { accept: 'application/json' }));
		expect(res.status).toBe(200); // resolves into the +server.ts route (which 401s on its own)
	});

	test('exempt: /auth/* (the login round-trip), _app assets, api/capi namespaces, extensions, non-GET, data requests', () => {
		const gated = (p: string, o: Parameters<typeof fakeEvent>[1] = {}) =>
			isGatedPageRequest(fakeEvent(p, o));
		expect(gated('/auth/login')).toBe(false);
		expect(gated('/data/_app/immutable/entry.js')).toBe(false);
		expect(gated('/admin/api/audit')).toBe(false);
		expect(gated('/data/capi/v1/warehouses')).toBe(false);
		expect(gated('/favicon.png')).toBe(false);
		expect(gated('/data/tables', { method: 'POST' })).toBe(false);
		expect(gated('/data/tables', { isDataRequest: true })).toBe(false);
		expect(gated('/data/tables')).toBe(true);
		expect(gated('/')).toBe(true); // the home zone's root is a page like any other
	});
});
