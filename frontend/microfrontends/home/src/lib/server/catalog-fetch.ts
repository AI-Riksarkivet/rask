import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';

// The zone's ONE door to the catalog for remote functions: forward the signed-in session's bearer,
// turn every outcome into the shared `ApiResult` union, and parse at the wire boundary.
//
// It lives here rather than inside a `.remote.ts` because a remote module may export only remote
// functions — and because this was three verbatim copies (`warehouses.remote.ts`,
// `access.remote.ts`, and the #65 policy module that would have been the third). The estate has
// already paid for that pattern once: `CONTROL_ID_RE` was three copies of an id-shape rule that had
// silently drifted on their end anchor. A transport helper drifts the same way, and the drift would
// be a status the caller's honest-state branch never sees.
//
// `$lib/server/` is deliberate: SvelteKit's illegal-import check makes the server-only boundary
// structural, so `$app/server` + `$env/dynamic/private` can never be pulled into a client bundle
// through a stray import.

/** The catalog's server-side base URL. Exported because a handful of calls go through `@rask/api`'s own
 *  client (`fetchMe`) rather than `catalogJSON`, and they must address the same catalog. */
export const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure detail.
 *
 *  An UNREACHABLE catalog is `{ok:false, status:0}` (frontend-conventions §1.0: a rejected fetch must
 *  never cross the remote boundary) — the pages read that status to render an honest "Catalog
 *  unreachable" instead of a boundary error, and a create dialog's `failText` distinguishes a client
 *  TIMEOUT (where the catalog may still have committed) from a refused connection off the same status. */
export async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `catalog answered ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	return { ok: true, data: await res.json() };
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}
