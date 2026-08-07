/**
 * The compute zone's upstream DOOR — over `@rask/api/upstream` (#93). One upstream (the catalog,
 * for the dock's two user-state documents), so one door. The implementation lives once in
 * `@rask/api/upstream`; what stays here is only what cannot move — `getRequestEvent` is app-bound,
 * and the base URL is this zone's own.
 *
 * `$lib/server/` on purpose (home's `catalog-fetch.ts` precedent): SvelteKit's illegal-import
 * check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type { ApiResult } from '@rask/api/client';
import { upstreamJSON } from '@rask/api/upstream';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** One catalog call → `ApiResult<unknown>`. The STATUS is load-bearing for the dock's three
 *  outcomes: 409 is "a document exists and cannot be read" (never overwrite), 401 is signed out,
 *  0 is unreachable — none of which may be flattened into "empty", the flattening that destroys a
 *  user's workspace. */
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
