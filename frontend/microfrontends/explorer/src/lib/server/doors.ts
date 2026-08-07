/**
 * The explorer's upstream DOORS — over `@rask/api/upstream` (#93).
 *
 * This zone carried THREE verbatim copies of the transport ladder (`catalog.remote.ts`,
 * `projects.remote.ts`, `labeling.remote.ts`), two of the write gate and two of the `typedAs`
 * cast. The implementation now lives once in `@rask/api/upstream`; what stays here is only what
 * cannot move — `getRequestEvent` is app-bound, and the zone's base URLs and gate stance are its
 * own.
 *
 * THREE doors because the zone speaks to three planes: the catalog (user-state + identity, OIDC
 * bearer only), and the annotator's projects and media planes — the projects/task plane may live
 * on a DIFFERENT annotator than the media plane (cluster actors + a locally-seeded corpus), the
 * dev seam the deleted routes carried; `ANNOTATOR_PROJECTS_API` falls back to `ANNOTATOR_API`,
 * one service in any real deploy.
 *
 * One deliberate change rides the migration: a 200 whose body is not JSON used to either pass as
 * data or throw across the remote boundary; the shared producer answers it as a 502 contract
 * drift, which the callers' failure branches already render.
 *
 * `$lib/server/` on purpose (home's `catalog-fetch.ts` precedent): SvelteKit's illegal-import
 * check makes the server-only boundary structural.
 */

import { getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import type { ApiResult } from '@rask/api/client';
import { upstreamJSON } from '@rask/api/upstream';

export const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';
export const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';
export const PROJECTS_API =
	env.ANNOTATOR_PROJECTS_API ?? env.ANNOTATOR_API ?? 'http://localhost:8103';
export const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

function doorTo(
	base: string,
	upstream: string,
	path: string,
	init?: RequestInit,
): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	return upstreamJSON({
		fetch,
		base,
		path,
		init,
		bearer: locals.session?.accessToken,
		upstream,
	});
}

/** One catalog call → `ApiResult<unknown>`. The STATUS is load-bearing for user-state and must
 *  survive the trip: 409 means "a document exists and cannot be read" (do not seed, do not
 *  overwrite), 401 means signed out (the mirror is the honest local answer), and an unreachable
 *  store is status 0 — none of which may be flattened into "empty", which is the flattening that
 *  destroys a user's work. */
export function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(CATALOG_API, 'catalog', path, init);
}

/** One projects-plane call → `ApiResult<unknown>`; the signed-in session's bearer rides along. */
export function projectsJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(PROJECTS_API, 'annotator', path, init);
}

/** One media-plane call into the annotator — same contract, the other base. */
export function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(ANNOTATOR_API, 'annotator', path, init);
}

/** One lance-viewer call (the graph console) — same contract, the viewer base. */
export function viewerJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return doorTo(VIEWER_API, 'viewer', path, init);
}

/** The write gate the deleted POST routes carried (`requireSession: true`): on an auth-enabled
 *  stack a write must be attributable — it seeds task provenance — so an anonymous caller is
 *  refused before the request leaves the zone. The reads had no such gate and still have none. */
export function sessionGate(): { ok: false; status: number; detail: string } | null {
	const { locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in required' };
	}
	return null;
}

/** The UNPARSED seam, in one place instead of three: these planes' own shapes are the contract
 *  (the deleted routes parsed nothing and neither did the clients), so the payload is handed on
 *  with the type the call site already used. This is a cast, not a check — #94 replaces it with
 *  real schemas through `@rask/api/upstream`'s `parsed`. */
export function typedAs<T>(result: ApiResult<unknown>): ApiResult<T> {
	return result.ok ? { ok: true, data: result.data as T } : result;
}
