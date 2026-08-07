/**
 * The ONE producer of `ApiResult` for a zone's server-side JSON reads (#93).
 *
 * Before this existed, `@rask/api` shipped the `ApiResult<T>` TYPE with no producer — so sixteen
 * remote modules across all seven zones hand-rolled the same try/fetch/status ladder, and the
 * copies drifted exactly as predicted: nine returned `status: 0` for an unreachable upstream, four
 * returned `502`, `models` rendered an "unreachable" branch its own transport could never produce,
 * and home's `catalog-fetch.ts` — whose header says it exists BECAUSE it was three copies — gained
 * a verbatim fourth copy in the same zone that drifted on non-JSON-200 handling within two days.
 *
 * The three outcomes, and they are a CONTRACT every consumer's honest-state branch reads:
 *   - `status: 0`   — the upstream is UNREACHABLE (rejected fetch). Never a throw: a rejection
 *                     crossing the remote boundary skips every caller's offline branch.
 *   - `status: n`   — the upstream ANSWERED n (its `{detail}` surfaced when it sent one).
 *   - `status: 502` — CONTRACT DRIFT: a 200 whose body is not JSON, or (via `parsed`) JSON that
 *                     fails its schema. Reserved for exactly this, so "the upstream is broken" and
 *                     "our contract with it is stale" stay distinguishable.
 *
 * Zone-bound inputs are ARGUMENTS by design: `getRequestEvent` exists only inside an app, so the
 * event's `fetch` and the session bearer are passed in. Each zone keeps a 2–4 line delegating
 * door; the implementation lives here once.
 */

import * as v from 'valibot';

import type { ApiResult } from './client.js';

/** One upstream JSON request — a parameter object because six positionals would be F1 soup. */
export interface UpstreamRequest {
	/** The REQUEST EVENT's fetch, so SvelteKit's server-side cookies/tracing ride along. */
	fetch: typeof globalThis.fetch;
	/** The upstream's base URL (env-resolved in the zone — each zone names its own). */
	base: string;
	path: string;
	init?: RequestInit | undefined;
	/** The signed-in session's bearer, when the caller has one. */
	bearer?: string | undefined;
	/** Names the upstream in failure details ("catalog answered 503") — a detail that cannot say
	 *  WHICH upstream failed sends the reader to the wrong service. */
	upstream: string;
}

/** One upstream call → the `ApiResult` union. Never throws. */
export async function upstreamJSON(req: UpstreamRequest): Promise<ApiResult<unknown>> {
	const { fetch, base, path, init, bearer, upstream } = req;
	let res: Response;
	try {
		res = await fetch(`${base}${path}`, {
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
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	// A 200 whose body is not JSON is CONTRACT DRIFT, not success. This guard is the exact line the
	// sixteenth copy (home's view-prefs) added locally and the canonical copy lacked — the drift
	// class this module exists to end, so the guard lives in the one implementation.
	try {
		return { ok: true, data: await res.json() };
	} catch (err) {
		return { ok: false, status: 502, detail: `${upstream} answered 200 with a non-JSON body: ${String(err)}` };
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

/**
 * The failure → user-copy ladder, written once.
 *
 * `status 401 → sign in / 403 → denied / 0 → unreachable / else → the upstream's own detail` was
 * re-derived in 26 files across six zones — one policy, 26 chances to disagree about what a status
 * means. `action` names what the person was doing ("load the queue"), because "denied" with no
 * object is a message nobody can act on.
 */
export function failText(fail: { status: number; detail: string }, action: string): string {
	if (fail.status === 401) return `Sign in to ${action}.`;
	if (fail.status === 403) return `You do not have permission to ${action}.`;
	if (fail.status === 0) return `Could not ${action} — the service is unreachable. ${fail.detail}`;
	return fail.detail;
}
