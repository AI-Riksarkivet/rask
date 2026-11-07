// @rask/api/client — the browser's one transport to the lakehouse's doors.
//
// The console is a single-page app with no backend of its own: every call leaves the browser for the
// same origin's `/api/*`, which the platform's Gateway routes to the service that owns the prefix
// (`/api/catalog` → the catalog, `/api/lineage` → lineage). The signed-in person's access token rides
// as a bearer, so each service verifies the caller and decides for itself; nothing in between holds a
// credential.
//
// The bearer comes from a PROVIDER the app installs once (`setBearerProvider`), so this package
// stays framework-free and holds no token: the provider reads the in-memory session the app's OIDC
// client keeps.
import * as v from 'valibot';

/** Per-request timeout — a hung backend must not stack poll ticks. */
export const FETCH_TIMEOUT_MS = 8000;

// AbortSignal.timeout needs Safari 16+/Chrome 103+; vite transpiles syntax, not APIs — on an older
// browser the missing function would throw inside a fetcher's catch and the app would silently render
// permanently "offline". Feature-detect with an AbortController fallback.
export function timeoutSignal(ms: number = FETCH_TIMEOUT_MS): AbortSignal {
	if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
	const controller = new AbortController();
	setTimeout(() => controller.abort(), ms);
	return controller.signal;
}

/** A fetch outcome that keeps the HTTP status — writes need 401 ("sign in") ≠ 403 (rung denial) ≠ 0
 * (offline), which getJSON-style null-on-error cannot express.
 *
 * The three outcomes are a CONTRACT every consumer's honest-state branch reads:
 *   - `status: 0`   — the upstream is UNREACHABLE (rejected fetch). Never a throw.
 *   - `status: n`   — the upstream ANSWERED n (its `{detail}` surfaced when it sent one).
 *   - `status: 502` — CONTRACT DRIFT: a 200 whose body is not JSON, or (via `parsed`) JSON that
 *                     fails its schema, so "the upstream is broken" and "our contract with it is
 *                     stale" stay distinguishable. */
export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string };

let bearerProvider: () => string | undefined = () => undefined;

/** Install the source of the signed-in person's access token (the app's in-memory OIDC session). */
export function setBearerProvider(provider: () => string | undefined): void {
	bearerProvider = provider;
}

/** The current access token, or `undefined` when signed out or on an auth-off stack. */
export function currentBearer(): string | undefined {
	return bearerProvider();
}

/** Whether `url` resolves to this page's own origin. Off a page (a unit test) only a relative URL does. */
function sameOrigin(url: string): boolean {
	if (typeof location === 'undefined') return url.startsWith('/') && !url.startsWith('//');
	try {
		return new URL(url, location.href).origin === location.origin;
	} catch {
		return false;
	}
}

/** `fetch` with the person's bearer attached — unless the caller set `authorization` itself, and only to
 *  this origin: the token is the lakehouse's, and no other origin is ever handed it, whatever the CSP. */
export function authorizedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers);
	const bearer = currentBearer();
	if (bearer && sameOrigin(input) && !headers.has('authorization')) {
		headers.set('authorization', `Bearer ${bearer}`);
	}
	return fetch(input, { ...init, headers });
}

/** Lift `detail` out of an error body, tolerating a non-JSON error page. */
function detailFrom(body: unknown, fallback: string): string {
	if (body !== null && typeof body === 'object' && 'detail' in body) {
		const detail = (body as { detail: unknown }).detail;
		return typeof detail === 'string' ? detail : JSON.stringify(detail);
	}
	return fallback;
}

/** One upstream JSON request — a parameter object because six positionals would be F1 soup. */
export interface UpstreamRequest {
	/** The fetch to use; `authorizedFetch` by default, so the person's bearer rides along. */
	fetch?: typeof globalThis.fetch | undefined;
	/** The upstream's same-origin base (`/api/catalog`, `/api/lineage`, …). */
	base: string;
	path: string;
	init?: RequestInit | undefined;
	/** An explicit bearer; omitted, the installed provider's is used. */
	bearer?: string | undefined;
	/** Names the upstream in failure details ("catalog answered 503") — a detail that cannot say
	 *  WHICH upstream failed sends the reader to the wrong service. */
	upstream: string;
}

/** One upstream call → the `ApiResult` union. Never throws. */
export async function upstreamJSON(req: UpstreamRequest): Promise<ApiResult<unknown>> {
	const { base, path, init, upstream } = req;
	const doFetch = req.fetch ?? globalThis.fetch;
	const bearer = sameOrigin(`${base}${path}`) ? (req.bearer ?? currentBearer()) : undefined;
	let res: Response;
	try {
		res = await doFetch(`${base}${path}`, {
			...init,
			headers: {
				...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
				...init?.headers,
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `${upstream} answered ${res.status}`;
		try {
			detail = detailFrom(await res.json(), detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	// A 200 whose body is not JSON is CONTRACT DRIFT, not success.
	try {
		return { ok: true, data: await res.json() };
	} catch (err) {
		return {
			ok: false,
			status: 502,
			detail: `${upstream} answered 200 with a non-JSON body: ${String(err)}`,
		};
	}
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
	upstream = 'upstream',
): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: v.parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `${upstream} contract drift: ${String(err)}` };
	}
}

/** The ONE declaration seam: stamp a verified-JSON `unknown` with the caller's wire type. Reserved
 *  for contracts that are MACHINE-GENERATED from a service's own OpenAPI (`generated/*.ts`); a
 *  hand-written shape calls the transport bare (`T = unknown`) and parses at its boundary. */
const declaredAs = <T>(body: unknown): T => body as T;

/** Status-aware JSON request against a same-origin API base (`/api/catalog`, `/api/lineage`). */
export async function requestJSON<T = unknown>(
	apiBase: string,
	path: string,
	init?: RequestInit,
): Promise<ApiResult<T>> {
	try {
		const res = await authorizedFetch(`${apiBase}/${path}`, {
			...init,
			signal: timeoutSignal(FETCH_TIMEOUT_MS),
		});
		const body: unknown = await res.json().catch(() => ({}));
		if (!res.ok)
			return { ok: false, status: res.status, detail: detailFrom(body, `HTTP ${res.status}`) };
		return { ok: true, data: declaredAs<T>(body) };
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
}

/** The binary sibling of `requestJSON` — for responses that are NOT JSON (an Arrow IPC file, blob
 *  bytes). A failure still reads the JSON `detail`, with the same 401 ≠ 403 ≠ 0 status split. */
export async function requestBinary(
	apiBase: string,
	path: string,
	init?: RequestInit,
): Promise<ApiResult<ArrayBuffer>> {
	try {
		const res = await authorizedFetch(`${apiBase}/${path}`, {
			...init,
			signal: timeoutSignal(FETCH_TIMEOUT_MS),
		});
		if (!res.ok) {
			const body: unknown = await res.json().catch(() => ({}));
			return { ok: false, status: res.status, detail: detailFrom(body, `HTTP ${res.status}`) };
		}
		return { ok: true, data: await res.arrayBuffer() };
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
}

/** BYTES UP, JSON ACK DOWN — the shape every data-plane write (an Arrow body, a `{version}` ack) has. */
export async function requestBytes(
	apiBase: string,
	path: string,
	body: BodyInit,
	contentType = 'application/vnd.apache.arrow.stream',
): Promise<ApiResult<unknown>> {
	try {
		const res = await authorizedFetch(`${apiBase}/${path}`, {
			method: 'POST',
			headers: { 'content-type': contentType },
			body,
			signal: timeoutSignal(FETCH_TIMEOUT_MS),
		});
		const payload: unknown = await res.json().catch(() => ({}));
		if (!res.ok)
			return { ok: false, status: res.status, detail: detailFrom(payload, `HTTP ${res.status}`) };
		return { ok: true, data: payload };
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
}

/** Null-on-any-error JSON read — for chrome and best-effort panels where an empty state and an
 *  outage read the same to the user. */
export async function getJSON<T>(apiBase: string, path: string): Promise<T | null> {
	const result = await requestJSON<T>(apiBase, path);
	return result.ok ? result.data : null;
}
