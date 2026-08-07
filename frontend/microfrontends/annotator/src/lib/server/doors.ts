/**
 * The annotator's upstream DOORS — over `@rask/api/upstream` (#93).
 *
 * This zone carried THREE verbatim copies of the transport ladder (`projects.remote.ts`,
 * `tasks.remote.ts`, `jobs.remote.ts`), plus two copies of the session guard and two of the
 * boundary parse. The implementation now lives once in `@rask/api/upstream`; what stays here is
 * only what cannot move — `getRequestEvent` is app-bound, and the zone's base URLs and the
 * `requireSession` stance are its own.
 *
 * TWO doors because the zone speaks to two planes: the projects/task plane may live on a
 * DIFFERENT annotator than the media plane (cluster actors + a locally-seeded corpus) — the dev
 * seam the deleted routes carried. `ANNOTATOR_PROJECTS_API` falls back to `ANNOTATOR_API`: one
 * service in any real deploy.
 *
 * One deliberate change rides the migration: the old copies answered a contract drift with
 * `status: 0` and a 200-with-non-JSON body as `{ok: true, data: {}}`. The shared producer says
 * 502 for both — every page here buckets non-401/403/404 failures into its offline branch, so
 * the rendered outcome is unchanged, and the drift's own words now reach the card.
 *
 * `$lib/server/` on purpose (home's `catalog-fetch.ts` precedent): SvelteKit's illegal-import
 * check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed as sharedParsed, upstreamJSON } from '@rask/api/upstream';

export const PROJECTS_API =
	env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';
export const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';
// The search service. Matches the explorer's own resolution (`SEARCH_API ?? :8102`) so the two
// zones cannot end up pointed at different services on the same stack.
export const SEARCH_API = env.SEARCH_API ?? 'http://localhost:8102';

function doorTo(base: string, path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream: 'annotator',
	});
}

/** One projects-plane call → `ApiResult<unknown>`; the signed-in session's bearer rides along. */
export function projectsJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(PROJECTS_API, path, init);
}

/** One media-plane call (batch labeling jobs, assist) — same contract, the other base. */
export function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(ANNOTATOR_API, path, init);
}

/** One search-service call ("more like this") — same contract, the search base. */
export function searchJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base: SEARCH_API,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream: 'search',
	});
}

/** The deleted routes' `requireSession: true`: on an auth-enabled stack a write never leaves this
 *  server without a signed-in user, and the caller sees the same 401 the BFF answered. */
export function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

export const SIGN_IN_REQUIRED: ApiResult<never> = {
	ok: false,
	status: 401,
	detail: 'sign in required',
};

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'annotator');
}
