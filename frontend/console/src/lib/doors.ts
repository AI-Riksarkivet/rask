/**
 * The console's door to the catalog: one call → the shared `ApiResult` union, the signed-in person's
 * bearer attached, the payload parsed at the wire boundary. The implementation is `@rask/api/client`'s
 * `upstreamJSON`; what stays here is only the base each upstream answers at (`#lib/upstreams`).
 *
 * The semantics are the shared contract — 0 unreachable, n answered-n with the upstream's `{detail}`,
 * 502 contract drift.
 */
import type * as v from 'valibot';
import { parsed as sharedParsed, upstreamJSON, type ApiResult } from '@rask/api/client';
import { CATALOG_API } from '#lib/upstreams.ts';

/** One catalog call → `ApiResult<unknown>`; the signed-in person's bearer rides along. */
export function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return upstreamJSON({ base: CATALOG_API, path, init, upstream: 'catalog' });
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'catalog');
}
