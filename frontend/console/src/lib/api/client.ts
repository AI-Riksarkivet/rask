// #lib/api/client — the browser's one transport to the lakehouse's doors.
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
//
// EVERY REQUEST LEAVES THROUGH `send`. It attaches the bearer, and when a door answers 401 to a request
// that carried one it asks the app to renew the session once (`setBearerRenewal`) and sends the request
// again with the new bearer. A 401 means the door did nothing, so a write is as safe to send again as a
// read. A request that carried no bearer, or one the caller authorized itself, is answered as it came.
// One renewal answers every request refused with the old bearer: a 401 that lands after the session was
// renewed is sent again with the bearer now held, and a bearer a door refused straight after a renewal is
// not renewed for again, so a door that refuses every token costs the issuer one exchange, not one a poll.
import * as v from 'valibot';

/** Per-request timeout — a hung backend must not stack poll ticks. */
export const FETCH_TIMEOUT_MS = 8000;

/** A write's timeout: an Arrow append the catalog commits slowly is still waited for, since a write given up on may
 *  land anyway, and the person is then told only that it is not confirmed ([[LH-577]]). */
export const WRITE_TIMEOUT_MS = 120_000;

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
export type ApiResult<T> = { ok: true; data: T } | ApiFailure;

/** A request that did not succeed. Beyond the status and the sentence it carries what a door's problem
 *  body says in members a client dispatches on (the doors answer RFC 9457 problem details): the spec's
 *  error `code`, the problem's `type`, and every member beyond the standard ones (`members`), such as
 *  `holds`, the children a refused delete names. A page branches on those and shows `detail`; it never
 *  reads a branch out of the sentence. */
export interface ApiFailure {
	ok: false;
	status: number;
	detail: string;
	/** The door's error code (`docs/catalog-openapi.json`, `ProblemDetails.code`), when it sent a problem. */
	code?: number;
	/** The problem's type URI, which tells apart refusals that share a code. */
	type?: string;
	/** The problem's extension members. */
	members?: Readonly<Record<string, unknown>>;
	/** No answer came because the request's own time ran out: unlike a refused connection, the door may
	 *  still have acted. Only on `status: 0`. */
	timedOut?: true;
}

/** The types of a delete refused because its object is protected against deletion: a table's, and a
 *  namespace's, warehouse's or project's. */
const PROTECTED_PROBLEMS: ReadonlySet<string> = new Set([
	'https://lance.org/problems/protectedtableerror',
	'https://lance.org/problems/protectedcontainererror',
]);

/** Whether a delete was refused because its object is protected against deletion. */
export function refusedAsProtected(failure: ApiFailure): boolean {
	return failure.type !== undefined && PROTECTED_PROBLEMS.has(failure.type);
}

/** The children a refused delete names (`holds`): what its container still holds. Empty when the
 *  refusal names none. */
export function heldChildren(failure: ApiFailure): string[] {
	const holds = failure.members?.['holds'];
	return Array.isArray(holds) ? holds.filter((id): id is string => typeof id === 'string') : [];
}

const PROBLEM_MEMBERS: ReadonlySet<string> = new Set([
	'type',
	'title',
	'status',
	'detail',
	'code',
	'error',
	'instance',
]);

/** A door's refusal: its status, and what its body says when the body is a problem. */
function refusal(status: number, body: unknown, fallback: string): ApiFailure {
	const failure: ApiFailure = { ok: false, status, detail: detailFrom(body, fallback) };
	if (body === null || typeof body !== 'object') return failure;
	const problem = body as Record<string, unknown>;
	if (typeof problem['code'] === 'number') failure.code = problem['code'];
	if (typeof problem['type'] === 'string') failure.type = problem['type'];
	const members = Object.fromEntries(
		Object.entries(problem).filter(([name]) => !PROBLEM_MEMBERS.has(name)),
	);
	if (Object.keys(members).length > 0) failure.members = members;
	return failure;
}

/** A request no answer came to. */
function unanswered(err: unknown, detail: string = String(err)): ApiFailure {
	const name = err instanceof Error ? err.name : '';
	const failure: ApiFailure = { ok: false, status: 0, detail };
	if (name === 'TimeoutError' || name === 'AbortError') failure.timedOut = true;
	return failure;
}

let bearerProvider: () => string | undefined = () => undefined;

/** Install the source of the signed-in person's access token (the app's in-memory OIDC session). */
export function setBearerProvider(provider: () => string | undefined): void {
	bearerProvider = provider;
}

/** The current access token, or `undefined` when signed out or on an auth-off stack. */
export function currentBearer(): string | undefined {
	return bearerProvider();
}

let renewBearer: () => Promise<boolean> = () => Promise.resolve(false);

/** Install what renews the signed-in person's session without leaving the page; it answers whether a
 *  new access token is now held. With none installed nothing is renewed and a 401 is answered as it came. */
export function setBearerRenewal(renew: () => Promise<boolean>): void {
	renewBearer = renew;
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

/** What one request is sent with, beyond its `RequestInit`. */
interface Sending {
	/** The fetch to use; the browser's by default. A load function passes its own. */
	fetch?: typeof globalThis.fetch | undefined;
	/** The bearer of the first attempt; omitted, the installed provider's. A retry after a renewal always
	 *  carries the provider's, which is the renewed one. */
	bearer?: string | undefined;
	/** How long each attempt may take. A retry after a renewal gets the whole of it again, so a slow
	 *  exchange at the issuer does not spend the request's own time. Omitted, `init.signal` alone bounds it. */
	timeoutMs?: number | undefined;
}

/** The bearer a door refused although it had just been renewed: renewing again would change nothing. */
let refusedAfterRenewal: string | undefined;

/** The one way a request leaves the browser. The person's bearer is attached unless the caller set
 *  `authorization` itself, and only to this origin: the token is the lakehouse's, and no other origin is
 *  ever handed it, whatever the CSP. A 401 to a request that carried the bearer renews the session once and
 *  sends the request again. */
async function send(input: string, init: RequestInit, sending: Sending = {}): Promise<Response> {
	const doFetch = sending.fetch ?? globalThis.fetch;
	const attempt = (bearer: string | undefined): Promise<Response> => {
		const headers = new Headers(init.headers);
		if (bearer) headers.set('authorization', `Bearer ${bearer}`);
		const signal = sending.timeoutMs ? timeoutSignal(sending.timeoutMs) : init.signal;
		return doFetch(input, { ...init, headers, ...(signal ? { signal } : {}) });
	};
	const mayCarry = sameOrigin(input) && !new Headers(init.headers).has('authorization');
	const bearer = mayCarry ? (sending.bearer ?? currentBearer()) : undefined;
	const answer = await attempt(bearer);
	if (answer.status !== 401 || !bearer) return answer;
	// Renewed meanwhile by another request's 401: the bearer now held has not been tried.
	const held = currentBearer();
	if (held && held !== bearer) return attempt(held);
	if (bearer === refusedAfterRenewal || !(await renewBearer())) return answer;
	const renewed = currentBearer();
	const again = await attempt(renewed);
	if (again.status === 401) refusedAfterRenewal = renewed;
	return again;
}

/** `fetch` with the person's bearer attached, through {@link send}. */
export function authorizedFetch(input: string, init: RequestInit = {}): Promise<Response> {
	return send(input, init);
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
	/** A timeout of the caller's own for each attempt, in place of a read's {@link FETCH_TIMEOUT_MS}. */
	timeoutMs?: number | undefined;
	/** Names the upstream in failure details ("catalog answered 503") — a detail that cannot say
	 *  WHICH upstream failed sends the reader to the wrong service. */
	upstream: string;
}

/** Whether a request only reads: a GET or HEAD, which is what a request naming no method is. */
function isRead(init: RequestInit | undefined): boolean {
	const method = (init?.method ?? 'GET').toUpperCase();
	return method === 'GET' || method === 'HEAD';
}

/** One upstream call → the `ApiResult` union. Never throws. */
export async function upstreamJSON(req: UpstreamRequest): Promise<ApiResult<unknown>> {
	const { base, path, init, upstream } = req;
	let res: Response;
	try {
		res = await send(
			`${base}${path}`,
			{
				...init,
				headers: {
					...(init?.body ? { 'content-type': 'application/json' } : {}),
					...init?.headers,
				},
			},
			{
				fetch: req.fetch,
				bearer: req.bearer,
				// A read gives up at a poll's timeout; a write is never cut off by one, since a write given up on may
				// land anyway, and the page would tell the person it was not applied.
				timeoutMs: req.timeoutMs ?? (isRead(init) && !init?.signal ? FETCH_TIMEOUT_MS : undefined),
			},
		);
	} catch (err) {
		return unanswered(err);
	}
	if (!res.ok) {
		// A non-JSON error body keeps the status-line detail.
		const body: unknown = await res.json().catch(() => null);
		return refusal(res.status, body, `${upstream} answered ${res.status}`);
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
		const res = await send(`${apiBase}/${path}`, init ?? {}, { timeoutMs: FETCH_TIMEOUT_MS });
		const body: unknown = await res.json().catch(() => ({}));
		if (!res.ok) return refusal(res.status, body, `HTTP ${res.status}`);
		return { ok: true, data: declaredAs<T>(body) };
	} catch (err) {
		return unanswered(err);
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
		const res = await send(`${apiBase}/${path}`, init ?? {}, { timeoutMs: FETCH_TIMEOUT_MS });
		if (!res.ok) {
			const body: unknown = await res.json().catch(() => ({}));
			return refusal(res.status, body, `HTTP ${res.status}`);
		}
		return { ok: true, data: await res.arrayBuffer() };
	} catch (err) {
		return unanswered(err);
	}
}

/** BYTES UP, JSON ACK DOWN — the shape every data-plane write (an Arrow body, a `{version}` ack) has.
 *
 * Waited for up to {@link WRITE_TIMEOUT_MS}. `idempotencyKey` makes a resend converge: the catalog answers a key it has
 * seen with that attempt's response instead of writing again, so a write that is retried after no answer keeps the key
 * it was first sent with. A write with no answer is `status: 0`, whose detail says it is not confirmed. */
export async function requestBytes(
	apiBase: string,
	path: string,
	body: BodyInit,
	options: { contentType?: string; idempotencyKey?: string } = {},
): Promise<ApiResult<unknown>> {
	const { contentType = 'application/vnd.apache.arrow.stream', idempotencyKey } = options;
	try {
		const res = await send(
			`${apiBase}/${path}`,
			{
				method: 'POST',
				headers: {
					'content-type': contentType,
					...(idempotencyKey ? { 'idempotency-key': idempotencyKey } : {}),
				},
				body,
			},
			{ timeoutMs: WRITE_TIMEOUT_MS },
		);
		const payload: unknown = await res.json().catch(() => ({}));
		if (!res.ok) return refusal(res.status, payload, `HTTP ${res.status}`);
		return { ok: true, data: payload };
	} catch (err) {
		return unanswered(err, `no answer came (${String(err)})`);
	}
}

/** Null-on-any-error JSON read — for chrome and best-effort panels where an empty state and an
 *  outage read the same to the user. */
export async function getJSON<T>(apiBase: string, path: string): Promise<T | null> {
	const result = await requestJSON<T>(apiBase, path);
	return result.ok ? result.data : null;
}
