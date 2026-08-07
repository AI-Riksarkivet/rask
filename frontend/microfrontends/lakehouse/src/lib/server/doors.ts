/**
 * The lakehouse's upstream DOORS — one per service, 3 lines each, over `@rask/api/upstream` (#93).
 *
 * This zone carried FIVE verbatim copies of the catalog ladder (the sixth transport, lineage's,
 * is a DIFFERENT thing — an adapter for `createLineageClient` with per-method auth and a documented
 * verbatim-offline sentence, and it stays where it is). The five had drifted, and
 * `catalog.remote.ts` and `warehouses.remote.ts` answered an UNREACHABLE upstream
 * with `status: 502` while their three siblings said `0` — so a page keyed on one status could sit
 * beside a page keyed on the other, and `dock/user-state-fetch.ts` fed the status into
 * `new Response(...)`, where 0 throws RangeError. The implementation now lives once in
 * `@rask/api/upstream`; what stays here is only what cannot move — `getRequestEvent` is app-bound,
 * and each upstream's base URL and auth shape are this zone's own.
 *
 * `$lib/server/` on purpose (home's `catalog-fetch.ts` precedent): SvelteKit's illegal-import check
 * makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed as sharedParsed, upstreamJSON } from '@rask/api/upstream';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** One catalog call → `ApiResult<unknown>`; the signed-in session's bearer rides along. */
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
