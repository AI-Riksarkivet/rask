import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import { ProjectsResponseSchema, type Project } from '$lib/admin/tenants';

// The tenants panel's seam to the catalog's first-class projects API, in the zone's remote-function
// dialect (open_transport.md, area 2) — same `ApiResult` shape at the call site as the `/api/projects`
// BFF route it replaces, transport only.
//
// Unlike the audit/jetstream seams (whose upstreams have no auth of their own) `/v1/projects` is
// estate-gated by the CATALOG's own FGA (`can_observe_events` on the root object), so this only
// bearer-forwards the signed-in user's token and passes the upstream verdict through: 401 anonymous on
// a governed stack, 403 for a non-estate-admin, the payload for an estate admin. There is deliberately
// no service-token fallback — the estate-wide tenant map must never be readable without an identity.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

/** Bounded: a wedged catalog must yield a fast honest failure, not pin the request. */
const TIMEOUT_MS = 8000;

/**
 * Every project the estate knows, with its warehouses and effective FGA admins.
 *
 * The wire parse moved HERE with the transport (the parse-don't-validate rule puts it at the
 * boundary, which is now the server). Its failure keeps the panel's own contract: `-1` is this
 * surface's long-standing "the payload drifted, refusing to render" status — deliberately not an HTTP
 * code, because a drift is not an outage and the panel renders the two differently.
 */
export const fetchProjects = query(async (): Promise<ApiResult<Project[]>> => {
	const { fetch, locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in to view tenants' };
	}
	const headers: Record<string, string> = locals.session
		? { authorization: `Bearer ${locals.session.accessToken}` }
		: {};
	let body: unknown;
	try {
		const upstream = await fetch(`${CATALOG_API}/v1/projects`, {
			headers,
			signal: AbortSignal.timeout(TIMEOUT_MS),
		});
		if (!upstream.ok) {
			const failure = (await upstream.json().catch(() => ({}))) as Record<string, unknown>;
			const detail =
				typeof failure.detail === 'string' ? failure.detail : `catalog answered ${upstream.status}`;
			return { ok: false, status: upstream.status, detail };
		}
		body = await upstream.json();
	} catch (err) {
		console.error(`tenants remote upstream failure: ${String(err)}`);
		return { ok: false, status: 502, detail: 'catalog unreachable' };
	}
	try {
		return { ok: true, data: parse(ProjectsResponseSchema, body) };
	} catch (err) {
		console.error(`tenants parse failure: ${String(err)}`);
		return { ok: false, status: -1, detail: 'the projects payload drifted from the contract' };
	}
});
