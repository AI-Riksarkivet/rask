import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { fetchMe, type Me } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import type { UserStateEnvelope } from '$lib/user-state';

// This zone's CATALOG plane, in the remote-function dialect (the transport ruling, area 3) — the two
// `makeCatalogProxy` routes (`capi/v1/me`, `capi/v1/user-state/[document]`) collapse into the three
// named functions below. Transport only: same credential (the signed-in user's bearer and NOTHING
// else — the catalog is OIDC-only, it has no service-token door), same upstream paths, same outcomes.
//
// Two properties the deleted `[document]` route argued for at length, and which the functions keep by
// construction rather than by convention:
//
//   • The reachable surface stays TWO documents. The route was a named `[document]` parameter rather
//     than a `capi/[...path]` catch-all precisely so this zone could not proxy warehouses, grants or
//     table mutation through its own origin; a picklist argument is the same fence, checked before the
//     request is built instead of after it is parsed.
//   • Identity is never in the path. The catalog derives the subject from the verified bearer; there is
//     no user parameter anywhere in this contract, and a function that accepted one would be a
//     cross-user read one missing check away.
//
// DELETE is not ported. The route exposed it and nothing in the estate ever called it — the same
// evidence that retires `api/jobs/[...path]` in this round. It comes back the day a caller does.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** The documents this zone owns — the picklist follows `UserStateDocument`. `dock-layout` +
 *  `dock-layout-library` back the zone's own search WORKBENCH (routes/workbench): its arrangement
 *  and its named views, per subject, on the same envelopes every dock in the estate uses. */
const DocumentArg = v.object({
	document: v.picklist(['workflow-graph', 'saved-views', 'dock-layout', 'dock-layout-library']),
});

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`. The STATUS is load-bearing for user-state and must survive
 *  the trip: 409 means "a document exists and cannot be read" (do not seed, do not overwrite), 401 means
 *  signed out (the mirror is the honest local answer), and an unreachable store is status 0 — none of
 *  which may be flattened into "empty", which is the flattening that destroys a user's work. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
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
		let detail = `the catalog answered ${res.status}`;
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

/**
 * The navbar's frozen `/v1/me` identity.
 *
 * Same degrade posture as before — `null` on ANY failure (signed out, 401, outage, timeout, contract
 * drift) — because identity is chrome: the navbar renders the base entry set and stays fail-closed on
 * the admin surfaces rather than 500ing the zone. The whole body is `@rask/api`'s shared `fetchMe`,
 * exactly as `feeds.remote.ts` is `@rask/api/runs-feed`: what cannot be shared is this file, because a
 * remote function must be declared inside an app to get its own endpoint.
 */
export const fetchMeViaBff = query(async (): Promise<Me | null> => {
	const { locals, fetch } = getRequestEvent();
	return fetchMe({
		catalogUrl: CATALOG_API,
		accessToken: locals.session?.accessToken,
		fetchFn: fetch,
	});
});

/** The catalog's own shape is the contract here (neither the deleted route nor the client parsed it);
 *  this hands the payload on with the type the caller already used. */
function typedAs<T>(result: ApiResult<unknown>): ApiResult<T> {
	return result.ok ? { ok: true, data: result.data as T } : result;
}

/** Read the caller's own document. The three-outcome mapping (ok / absent / unreadable) and the
 *  localStorage mirror stay in `$lib/user-state`, which is where they were and where they are tested. */
export const readUserStateDoc = query(
	DocumentArg,
	async ({ document }): Promise<ApiResult<UserStateEnvelope>> =>
		typedAs<UserStateEnvelope>(await catalogJSON(`/v1/user-state/${document}`)),
);

/** Save the caller's own document. A refused write is reported as such — a mirror write is not a save.
 *
 *  No single-flight refresh of `readUserStateDoc`: the writer already holds the value it just stored, so
 *  a refresh would spend a round trip per autosave to learn what the caller told the server. The reads
 *  that matter happen once, at mount. */
export const writeUserStateDoc = command(
	v.object({ ...DocumentArg.entries, value: v.unknown() }),
	async ({ document, value }): Promise<ApiResult<unknown>> =>
		catalogJSON(`/v1/user-state/${document}`, { method: 'PUT', body: JSON.stringify(value) }),
);
