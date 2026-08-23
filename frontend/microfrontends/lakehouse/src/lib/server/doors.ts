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

/** The GATEWAY, absolute — never a relative `/api/…` from this zone.
 *
 * `vite.config.ts` proxies this zone's `/api` to `LANCE_BACKEND` (:8001, the LINEAGE service), so a
 * relative call from here reaches the wrong backend in dev and only works in prod because the chart
 * happens to aim both names at one Service. Server-side the hairpin rewrite exists too, but it keys
 * on `LANCE_GATEWAY_URL`. Naming `RASK_GATEWAY_URL` here is the one form that is right in both.
 *
 * Separate from `catalogJSON` because it is a different upstream with a different base — the
 * promotion doors are ROOT-mounted on the medallion producer and reached through the gateway's
 * `/api/promotions` row, not through the catalog. */
const GATEWAY_API = env.RASK_GATEWAY_URL ?? 'http://localhost:8888';

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

/** One gateway call → `ApiResult<unknown>`; the signed-in session's bearer rides along.
 *
 * The bearer is not optional decoration here. `POST /promotions/{id}/decision` is gated on
 * `can_promote` — the VALIDATOR rung — and the producer REFUSES a service token outright, so the
 * estate's shared credential cannot approve its own output. Only a signed-in human's bearer can
 * decide, which is exactly what a remote function forwards. */
export function gatewayJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base: GATEWAY_API,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream: 'gateway',
	});
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'catalog');
}

/** `parsed` for a GATEWAY payload — same contract, honest attribution.
 *
 * Separate rather than a parameter with a default, because the upstream NAME is what a 502 reports:
 * a promotions drift labelled `catalog` would send the next reader to the wrong service. */
export function parsedGateway<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'gateway');
}
