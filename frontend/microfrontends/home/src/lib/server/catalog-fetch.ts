import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed as sharedParsed, upstreamJSON } from '@rask/api/upstream';

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

/** One catalog call → `ApiResult<unknown>` — a 3-line DOOR over `@rask/api/upstream` (#93).
 *
 *  The implementation moved to the shared producer once sixteen copies of it had drifted across the
 *  estate (nine said unreachable = 0, four said 502, and the non-JSON-200 guard existed in exactly
 *  one). What stays here is only what CANNOT move: `getRequestEvent` exists inside an app, so the
 *  event's fetch and the session bearer are read here and passed in. The semantics are the shared
 *  contract — 0 unreachable, n answered-n with the upstream's `{detail}`, 502 contract drift. */
export function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base: CATALOG_API,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream: 'catalog',
	});
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'catalog');
}
