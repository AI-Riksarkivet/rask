// @rask/api/bff — the SvelteKit BFF integration for the OIDC seam (server-only). Env-free: each app
// passes its own $env values in, so this is the shared, single-sourced auth trailer (mirroring the
// backend services' shared auth). Imports @sveltejs/kit TYPES only, so it lives in server bundles.
import type { Handle, RequestHandler } from '@sveltejs/kit';
import { makeGatewayHandleFetch } from './gateway';
import { fetchMe } from './me';
import {
	SESSION_COOKIE,
	decodeSession,
	deriveSessionKey,
	isExpired,
	type OidcConfig,
	type Session,
} from './oidc';

type Env = Record<string, string | undefined>;

/** The identity the shared AppShell's nav-user renders — structurally the `@rask/ui` shell's `NavUser`
 *  (name/email/initials), produced here (the data seam) and consumed there (the view), no cross-package dep. */
export interface SessionUser {
	name: string;
	email?: string;
	initials?: string;
}

/** Up-to-two-letter initials for the avatar fallback: first letter of the first two words, else the first
 *  two characters — always uppercased. */
function initialsOf(name: string): string {
	const words = name.trim().split(/\s+/).filter(Boolean);
	const letters =
		words.length >= 2 ? words[0]![0]! + words[1]![0]! : (words[0] ?? name).slice(0, 2);
	return letters.toUpperCase();
}

/** Project the OIDC session onto the shell identity, or `null` when signed out. Single-sourced so every
 *  zone's `+layout.server.ts` derives the same `user` — the "auth is identical in every MFE" seam. */
export function sessionToUser(session: Session | null): SessionUser | null {
	if (!session) return null;
	const name = session.name || session.email || session.sub;
	const user: SessionUser = { name, initials: initialsOf(name) };
	// exactOptionalPropertyTypes: only set `email` when present (never assign an explicit undefined).
	if (session.email) user.email = session.email;
	return user;
}

/** The auth fields the shared handle/proxy read on `event.locals`. Each zone's `app.d.ts` declares
 *  `interface Locals extends AuthLocals {}` so its RequestEvent carries them. */
export interface AuthLocals {
	session: Session | null;
	authEnabled: boolean;
}

/** Build the OIDC config from a plain env record, or `null` when unconfigured (→ the zone runs auth-OFF).
 *  Server-only (derives the AES key via node:crypto). */
export function makeOidcConfig(env: Env): OidcConfig | null {
	const issuer = env.OIDC_ISSUER;
	const clientId = env.OIDC_CLIENT_ID;
	const redirectUri = env.OIDC_REDIRECT_URI;
	if (!issuer || !clientId || !redirectUri) return null;
	return {
		issuer: issuer.replace(/\/$/, ''),
		clientId,
		clientSecret: env.OIDC_CLIENT_SECRET || null,
		redirectUri,
		scopes: env.OIDC_SCOPES || 'openid profile email',
		// SESSION_SECRET seals the cookie (AES-256-GCM); unset in dev → unsealed base64 (documented).
		sessionKey: env.SESSION_SECRET ? deriveSessionKey(env.SESSION_SECRET) : null,
		// Split-horizon: server-side fetches (discovery, token POST) go here when set — the in-cluster Dex
		// Service — while the browser's authorize redirect + iss keep the public OIDC_ISSUER above.
		internalIssuer: env.OIDC_INTERNAL_ISSUER?.replace(/\/$/, '') || null,
	};
}

/** Is this request a signed-out browser PAGE navigation the login-first gate should redirect? Central so
 *  all zones share the exact predicate. Gated (all must hold): a GET that is not a SvelteKit `__data.json`
 *  data request, whose `Accept` includes text/html (fetch/API clients don't send it — API `+server.ts`
 *  routes keep returning 401 JSON, a redirect would corrupt them), outside `/auth/*` (the login round-trip
 *  itself), outside the `_app`/`api`/`capi` namespaces at any base path, and not an asset-shaped path with
 *  a file extension (favicon.png, robots.txt, service-worker.js — normally served before the handle, but
 *  belt-and-braces). Exported for tests. */
export function isGatedPageRequest(event: {
	url: URL;
	request: Request;
	isDataRequest: boolean;
}): boolean {
	const { pathname } = event.url;
	if (event.request.method !== 'GET' || event.isDataRequest) return false;
	if (!(event.request.headers.get('accept') ?? '').includes('text/html')) return false;
	if (pathname === '/auth' || pathname.startsWith('/auth/')) return false;
	if (/\/(?:_app|api|capi)(?:\/|$)/.test(pathname)) return false;
	if (/\.[a-z0-9]+$/i.test(pathname)) return false;
	return true;
}

/** Per-request `handle`: hydrate `locals.session` from the sealed cookie, dropping a stale/dead one so a
 *  dead token is never forwarded. No-op when `cfg` is null (auth off). Single-sourced so every zone shares
 *  identical session semantics.
 *
 *  Login-first gate: when OIDC IS configured, a signed-out browser page navigation is redirected to the
 *  home zone's `/auth/login?redirect=<original path>` (the `?redirect=` contract nav-user.svelte already
 *  uses), so the login page is the first thing a signed-out user sees — on every zone, from this one
 *  change. With no OIDC env (dev servers, hermetic e2e) `cfg` is null and behavior is unchanged. A plain
 *  302 Response (not kit's `redirect()`) keeps this module's types-only @sveltejs/kit import. */
export function makeSessionHandle(cfg: OidcConfig | null): Handle {
	return async ({ event, resolve }) => {
		// App.Locals is declared per-zone; in @rask/api's own typecheck it's empty, so read via AuthLocals.
		const locals = event.locals as unknown as AuthLocals;
		locals.authEnabled = cfg !== null;
		locals.session = null;
		const raw = event.cookies.get(SESSION_COOKIE);
		if (cfg && raw) {
			const session = decodeSession(raw, cfg.sessionKey);
			if (session && !isExpired(session, Math.floor(Date.now() / 1000))) {
				locals.session = session;
			} else {
				event.cookies.delete(SESSION_COOKIE, { path: '/' });
			}
		}
		if (cfg && !locals.session && isGatedPageRequest(event)) {
			const returnTo = event.url.pathname + event.url.search;
			return new Response(null, {
				status: 302,
				headers: { location: `/auth/login?redirect=${encodeURIComponent(returnTo)}` },
			});
		}
		return resolve(event);
	};
}

/** A zero-width `stripPrefix` for backends that serve their routes UNDER `/api` (the lance-media
 *  viewer/search/annotator services): it matches at the start of the app-relative `/api/...` path —
 *  so the proxy's base-aware strip still removes the zone base when present — but strips NOTHING
 *  itself, forwarding `/explorer/api/health` (or dev's `/api/health`) as `/api/health` upstream. */
export const KEEP_API_PREFIX = /^(?=\/api\/)/;

/** Response headers never forwarded from an upstream: `fetch()` auto-decompresses the body, so the
 *  upstream's content-encoding/content-length describe bytes that no longer exist (forwarding them makes
 *  the browser gunzip plain bytes — ERR_CONTENT_DECODING_FAILED); the rest are hop-by-hop. */
const DROPPED_RESPONSE_HEADERS = new Set([
	'content-encoding',
	'content-length',
	'transfer-encoding',
	'connection',
	'keep-alive',
]);

/** A same-origin backend-proxy `RequestHandler` factory. Forwards the signed-in user's access token as a
 *  bearer (so the backend verifies + FGA-authorizes the call); a READ-only service-credential fallback lets
 *  a governed stack serve reads without a per-user login — NEVER on writes (the confused-deputy guard). */
export function makeBackendProxy(opts: {
	backendUrl: string;
	stripPrefix: RegExp;
	serviceToken?: string | undefined;
	serviceId?: string | undefined;
	/** Extra request headers (lowercase) forwarded to the backend — e.g. `['range', 'accept']` so media
	 *  streaming keeps HTTP Range seeking through the proxy. Default: none (auth + content-type only). */
	forwardRequestHeaders?: readonly string[] | undefined;
	/** Pass the upstream's response headers through (minus content-encoding/content-length + hop-by-hop) —
	 *  needed when the payload contract lives in headers (content-range/accept-ranges on media streams,
	 *  X-Annotations-Version on Arrow reads). Default: content-type only. */
	forwardResponseHeaders?: boolean | undefined;
	/** Fail closed on an auth-enabled stack with no signed-in user (401 without the request leaving the
	 *  BFF) — the write-route stance: a write must be attributable to a real user. Default: forward. */
	requireSession?: boolean | undefined;
}): RequestHandler {
	return async ({ url, fetch, request, locals }) => {
		// Base-aware strip: under the patched svelte-adapter-bun the BUILT server sees the zone's
		// BASED pathname (/data/capi/…) while dev sees the app-relative one (/capi/…) — so if the
		// prefix doesn't match at position 0, drop the single zone-base segment and re-try; without
		// this the full based path is forwarded verbatim and the backend 404s it (found live
		// 2026-07-24, the first image rebuild after the adapter patch landed with the media fold).
		const raw = url.pathname;
		const appPath = opts.stripPrefix.test(raw) ? raw : raw.replace(/^\/[^/]+(?=\/)/, '');
		const target = opts.backendUrl + appPath.replace(opts.stripPrefix, '') + url.search;
		const isRead = request.method === 'GET' || request.method === 'HEAD';
		const auth = locals as unknown as AuthLocals;
		if (opts.requireSession && auth.authEnabled && !auth.session) {
			return new Response(JSON.stringify({ detail: 'sign in required' }), {
				status: 401,
				headers: { 'content-type': 'application/json' },
			});
		}
		const headers: Record<string, string> = {};
		for (const name of opts.forwardRequestHeaders ?? []) {
			const value = request.headers.get(name);
			if (value !== null) headers[name] = value;
		}
		const session = auth.session;
		if (session) {
			headers['authorization'] = `Bearer ${session.accessToken}`;
		} else if (opts.serviceToken && isRead) {
			headers['dapr-api-token'] = opts.serviceToken;
			headers['x-lance-service-identity'] = opts.serviceId ?? '';
		}
		const init: RequestInit = isRead
			? { method: request.method, headers }
			: {
					method: request.method,
					headers: {
						...headers,
						'content-type': request.headers.get('content-type') ?? 'application/json',
					},
					// Bytes, not text: a multipart body (image search, voice upload) is binary — a
					// text round-trip would corrupt it.
					body: await request.arrayBuffer(),
				};
		try {
			const upstream = await fetch(target, init);
			let responseHeaders: HeadersInit;
			if (opts.forwardResponseHeaders) {
				const passed = new Headers();
				upstream.headers.forEach((value, name) => {
					if (!DROPPED_RESPONSE_HEADERS.has(name.toLowerCase())) passed.set(name, value);
				});
				responseHeaders = passed;
			} else {
				responseHeaders = {
					'content-type': upstream.headers.get('content-type') ?? 'application/json',
				};
			}
			return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
		} catch (err) {
			return new Response(JSON.stringify({ error: String(err) }), {
				status: 502,
				headers: { 'content-type': 'application/json' },
			});
		}
	};
}

// ─── Zone wiring ──────────────────────────────────────────────────────────────────────────────────
// Every zone's `hooks.server.ts`, `+layout.server.ts` and its two catch-all BFF proxy routes used to
// be byte-identical files copy-pasted per zone (with a second, also byte-identical, variant across the
// media zones). The factories below are the single source; a zone's file is now the wiring line plus
// its own `$env` — which is the ONE thing that genuinely cannot be hoisted, since `$env/dynamic/private`
// is a SvelteKit virtual module and this package stays env-free on purpose.

/** Default upstreams — the local dev topology. In-cluster the chart sets each `*_API` explicitly. */
const DEFAULT_CATALOG_API = 'http://localhost:2333';
const DEFAULT_LINEAGE_API = 'http://localhost:8001';
const DEFAULT_VIEWER_API = 'http://localhost:8101';
const DEFAULT_GATEWAY_URL = 'http://localhost:8001';

/** The cookie that carries the ACTIVE PROJECT across zones (#103). Set by the home zone when a
 *  project is opened (`/projects/[project]`), read by EVERY zone's layout so the shared shell can
 *  name the project you are inside and route back to it. Host-wide (`path=/`) on purpose: the
 *  zones are separate documents on one host, and a per-zone cookie would forget the project at
 *  every cross-zone hop — the exact defect this exists to fix. */
export const ACTIVE_PROJECT_COOKIE = 'rask_active_project';

type CookieReader = { get(name: string): string | undefined };

/**
 * The `load` every zone's `+layout.server.ts` re-exports: surface the signed-in identity + the
 * auth-enabled flag to the shared AppShell/TopNavbar (nav-user renders Sign in / Sign out). Derived
 * through `sessionToUser` so every zone produces the SAME user — "auth is identical in every MFE".
 * No-op shape on an auth-off stack: `user` null, `authEnabled` false.
 *
 * Also surfaces `activeProject` (#103): every zone visit happens INSIDE the active project, so the
 * shell must know it on the SERVER render — a client-side read paints "Default" first and the real
 * project after hydration, which is the flash-and-lie this replaced.
 */
export const zoneLayoutLoad = ({
	locals,
	cookies,
}: {
	locals: AuthLocals;
	cookies?: CookieReader;
}) => ({
	user: sessionToUser(locals.session),
	authEnabled: locals.authEnabled,
	activeProject: cookies?.get(ACTIVE_PROJECT_COOKIE) ?? '',
});

/**
 * The WHOLE zone layout contract, as a factory — identity, auth flag, active project AND the
 * RESOLVED `me` — owned here so all seven zones render one shell BY CONSTRUCTION.
 *
 * `zoneLayoutLoad` above carries no `me`, so every zone that wanted a truthful navbar bolted on its
 * own byte-identical `CATALOG_API` + `fetchMe` block — and the three that did not (compute, models,
 * studio) shipped a placeholder identity, no Settings entry, and a project switcher whose dropdown
 * listed nothing. A shared contract that each zone must remember to extend is not a contract.
 *
 * Resolved, never streamed (home's own precedent): the navbar SSRs its final entry set, so a
 * cross-zone landing paints the finished bar instead of skeleton pills that swap on hydration.
 * `fetchMe` times out internally and answers null — a slow catalog degrades the chrome, never the
 * page. Env stays zone-supplied (this module is env-free), mirroring `makeZoneHooks`.
 *
 * `params.project` wins over the cookie for ONE request: the layout load runs before the project
 * page's load stamps the cookie, so on the very first open of a project the route param is the
 * truth. Owning that precedence here keeps "what is the active project" a single rule.
 */
export function makeZoneLayoutLoad(env: Env) {
	const catalogUrl = env.CATALOG_API ?? DEFAULT_CATALOG_API;
	return async ({
		locals,
		cookies,
		params,
	}: {
		locals: AuthLocals;
		cookies: CookieReader;
		params: Partial<Record<string, string>>;
	}) => ({
		user: sessionToUser(locals.session),
		authEnabled: locals.authEnabled,
		activeProject: params.project ?? cookies.get(ACTIVE_PROJECT_COOKIE) ?? '',
		me: await fetchMe({ catalogUrl, accessToken: locals.session?.accessToken }),
	});
}

/**
 * The server hooks every zone re-exports.
 *
 * `handle` hydrates the session per request from the sealed OIDC cookie and runs the login-first gate;
 * it is a no-op when OIDC is unconfigured (the zone runs auth-off — dev servers and hermetic e2e are
 * unchanged). `handleFetch` rewrites SSR `/api/*` to the in-cluster gateway and is only wired for the
 * zones that read the lineage plane during SSR (`gateway: true`); the media zones reach their services
 * through their own BFF routes instead, so they leave it undefined.
 */
export function makeZoneHooks(env: Env, opts: { gateway?: boolean } = {}) {
	return {
		handle: makeSessionHandle(makeOidcConfig(env)),
		handleFetch: opts.gateway
			? makeGatewayHandleFetch(env.LANCE_GATEWAY_URL ?? DEFAULT_GATEWAY_URL)
			: undefined,
	};
}

/**
 * `capi/**` → the CATALOG service. The catalog is OIDC-only (no service-token door), so the only
 * credential ever attached is the signed-in user's bearer. GET-only: the same confused-deputy stance as
 * rask/apps/web — no blanket write proxy. Serves the frozen `/capi/v1/me` identity pass-through in every
 * zone, plus the catch-all in the zones that read the catalog directly. Anon stays the catalog's honest
 * 401 and the navbar renders signed-out chrome.
 */
export function makeCatalogProxy(env: Env): RequestHandler {
	return makeBackendProxy({
		backendUrl: env.CATALOG_API ?? DEFAULT_CATALOG_API,
		stripPrefix: /^\/capi/,
	});
}

/**
 * `api/**` → the LINEAGE service (the `/capi` proxy covers catalog). GET-only, forwards the signed-in
 * user's bearer; a READ-only service-credential fallback lets a governed stack serve reads without a
 * per-user login (never on writes — the confused-deputy guard).
 */
export function makeLineageProxy(env: Env): RequestHandler {
	return makeBackendProxy({
		backendUrl: env.LINEAGE_API ?? DEFAULT_LINEAGE_API,
		stripPrefix: /^\/api/,
		serviceToken: env.LINEAGE_SERVICE_TOKEN,
		serviceId: env.LINEAGE_SERVICE_ID,
	});
}

/**
 * `api/**` → the VIEWER service: the media-plane catch-all (documents, media/thumbnail/chunk-frame
 * streams, atlas, graph, voice, topics, datasets, health). GET-only, forwarding the signed-in user's
 * bearer so the backend can attribute the caller. Range rides through (media seeking) and the upstream's
 * response headers pass back (content-range/accept-ranges/cache-control). The search + annotations/
 * assist/jobs domains have their own more-specific routes beside this one.
 */
export function makeViewerProxy(env: Env): RequestHandler {
	return makeBackendProxy({
		backendUrl: env.VIEWER_API ?? DEFAULT_VIEWER_API,
		stripPrefix: KEEP_API_PREFIX,
		forwardRequestHeaders: ['range', 'accept', 'if-none-match', 'if-modified-since'],
		forwardResponseHeaders: true,
		// The catch-all served the corpus to anyone. Measured on the live estate:
		// `GET /annotator/api/atlas/points?space=text` returned `200` and **6,678,928 bytes** with no
		// session — the identical payload, from the identical upstream, one URL away from the media route
		// that had just been gated. Gating a URL is not gating a resource, and this helper is the resource:
		// both the media and annotator zones mount it as their media-plane catch-all, so the door closes in
		// both at once. `atlas/chunks` beside it already required a session, which is what made the gap
		// visible as an inconsistency rather than a policy.
		requireSession: true,
	});
}
