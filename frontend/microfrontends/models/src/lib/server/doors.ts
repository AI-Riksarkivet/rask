/**
 * The models zone's upstream DOOR — over `@rask/api/upstream` (#93). One upstream (the catalog's
 * model registry), so one door. The implementation lives once in `@rask/api/upstream`; what stays
 * here is only what cannot move — `getRequestEvent` is app-bound, and the base URL is this zone's
 * own.
 *
 * One deliberate change rides the migration: this zone's old ladder answered an UNREACHABLE
 * catalog with `status: 502` (what the deleted routes said). The shared contract says 0 — and the
 * registry surfaces were already taught to bucket BOTH into their offline state in the audit wave
 * (`![200, 401, 501].includes(lastStatus)` in `Experiments.svelte`, `lastStatus !== 401` in
 * `ModelRegistry.svelte`), so the rendered outcome is unchanged.
 *
 * `$lib/server/` on purpose (home's `catalog-fetch.ts` precedent): SvelteKit's illegal-import
 * check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed as sharedParsed, upstreamJSON } from '@rask/api/upstream';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** The GATEWAY, absolute — never a relative `/api/…`.
 *
 * Same rule the bell already follows in this zone (`lib/live/inbox.remote.ts`): a relative call is
 * resolved against the incoming external origin under svelte-adapter-bun, and only the zones that
 * proxy `/api` would reach anything at all. Naming the gateway is the one form that is right in dev
 * and in cluster. */
const GATEWAY_API = (env.RASK_GATEWAY_URL ?? 'http://localhost:8888').replace(/\/$/, '');

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
 * The bearer is the whole authorization story for `/train`: the door takes a service token OR an
 * ADMIN of the configured project, and a browser has only the latter. A remote function forwards
 * exactly the signed-in human's bearer — nothing else this zone holds would be accepted. */
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
 * A separate function rather than a defaulted parameter: the upstream NAME is what a 502 reports,
 * and a `/train` drift labelled `catalog` sends the next reader to the wrong service. */
export function parsedGateway<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'gateway');
}
