// @rask/api/client — the BROWSER side of a zone's own BFF: base-path-aware fetch helpers with a
// shared timeout and a status-aware result type.
//
// Every zone is served UNDER a base path (`/data`, `/admin`, `/media`, …) and its BFF proxy routes
// (`/api/*`, `/capi/*`) live there, so a browser call MUST carry that base — the Ingress path-routes
// `/<zone>` to this zone and a bare `/capi` 404s before it ever reaches us. The base is the only
// per-zone variable, so this module takes it as a parameter (`createBffClient`) instead of importing
// `$app/paths`: same env-free seam as the rest of @rask/api, which keeps the package framework-
// agnostic and unit-testable. Each zone binds it once in `src/lib/http.ts`.
//
// Server-safe too (fetch + valibot only, no node:crypto / $env), so a universal load may use it.
import { MeSchema, type Me } from './me';
import { parse } from './parse';

/** Per-request timeout — a hung backend must not stack poll ticks (§2 perf, 2026-07-11). */
export const FETCH_TIMEOUT_MS = 8000;

// AbortSignal.timeout needs Safari 16+/Chrome 103+; vite transpiles syntax, not APIs — on an older
// browser the missing function would throw inside a fetcher's catch and the app would silently render
// permanently "offline". Feature-detect with an AbortController fallback (review 2026-07-11).
export function timeoutSignal(ms: number = FETCH_TIMEOUT_MS): AbortSignal {
	if (typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
	const controller = new AbortController();
	setTimeout(() => controller.abort(), ms);
	return controller.signal;
}

/** A fetch outcome that keeps the HTTP status — writes need 401 ("sign in") ≠ 403 (rung denial) ≠ 0
 * (offline), which getJSON-style null-on-error cannot express. Shared by the lineage + catalog clients. */
export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; detail: string };

/** The per-zone browser client. Plain closures, not methods, so a zone can destructure the whole
 *  surface into named module exports without losing `this`. */
export interface BffClient {
	/** Prefix this zone's base path to an absolute same-origin BFF path (`/api/…`, `/capi/…`). */
	bffPath: (path: string) => string;
	/** Null-on-any-error JSON read under `/api` — for chrome and best-effort panels where an empty
	 *  state and an outage read the same to the user. */
	getJSON: <T>(path: string) => Promise<T | null>;
	/** Status-aware JSON request against a same-origin BFF base ("/api" or "/capi"). */
	requestJSON: <T>(base: string, path: string, init?: RequestInit) => Promise<ApiResult<T>>;
	/** The binary sibling of `requestJSON` — for BFF responses that are NOT JSON (the table-preview
	 *  Arrow-IPC file). Success hands back the raw bytes; a failure still reads the JSON `detail`
	 *  (error responses stay JSON on every BFF route), with the same 401 ≠ 403 ≠ 0 status split. */
	requestBinary: (
		base: string,
		path: string,
		init?: RequestInit,
	) => Promise<ApiResult<ArrayBuffer>>;
	/**
	 * Browser-side `/v1/me` through THIS zone's bearer-forwarding BFF pass-through (`/capi/v1/me`).
	 * Same degrade posture as the server-side `fetchMe`: `null` on ANY failure (signed out, 401,
	 * outage, timeout, contract drift) — the navbar then renders the base entry set, fail-closed on
	 * the admin surfaces. Browser-side on purpose: the session bearer lives in the sealed cookie the
	 * BFF holds, and a same-origin fetch keeps the identity seam mockable in hermetic e2e.
	 */
	fetchMeViaBff: () => Promise<Me | null>;
}

/** Bind the zone's SvelteKit `base` once (see `src/lib/http.ts` in every zone). */
export function createBffClient(base: string): BffClient {
	const prefix = base.replace(/\/$/, '');
	const bffPath = (path: string): string => `${prefix}${path}`;

	/** Lift `detail` out of an error body, tolerating a non-JSON error page. */
	const detailFrom = (body: Record<string, unknown>, status: number): string =>
		typeof body.detail === 'string' ? body.detail : `HTTP ${status}`;

	const requestJSON = async <T>(
		apiBase: string,
		path: string,
		init?: RequestInit,
	): Promise<ApiResult<T>> => {
		try {
			const res = await fetch(`${bffPath(apiBase)}/${path}`, {
				...init,
				signal: timeoutSignal(FETCH_TIMEOUT_MS),
			});
			const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
			if (!res.ok) return { ok: false, status: res.status, detail: detailFrom(body, res.status) };
			return { ok: true, data: body as T };
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}
	};

	const requestBinary = async (
		apiBase: string,
		path: string,
		init?: RequestInit,
	): Promise<ApiResult<ArrayBuffer>> => {
		try {
			const res = await fetch(`${bffPath(apiBase)}/${path}`, {
				...init,
				signal: timeoutSignal(FETCH_TIMEOUT_MS),
			});
			if (!res.ok) {
				const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
				return { ok: false, status: res.status, detail: detailFrom(body, res.status) };
			}
			return { ok: true, data: await res.arrayBuffer() };
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}
	};

	const getJSON = async <T>(path: string): Promise<T | null> => {
		try {
			const res = await fetch(bffPath(`/api/${path}`), { signal: timeoutSignal(FETCH_TIMEOUT_MS) });
			if (!res.ok) return null;
			return (await res.json()) as T;
		} catch {
			return null;
		}
	};

	const fetchMeViaBff = async (): Promise<Me | null> => {
		try {
			const res = await fetch(bffPath('/capi/v1/me'), { signal: timeoutSignal(FETCH_TIMEOUT_MS) });
			if (!res.ok) return null;
			return parse(MeSchema, await res.json());
		} catch {
			return null;
		}
	};

	return { bffPath, getJSON, requestJSON, requestBinary, fetchMeViaBff };
}
