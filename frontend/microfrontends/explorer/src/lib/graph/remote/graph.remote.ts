import { command, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import { GraphCypherResponseSchema, type GraphCypherResponse } from '@rask/media-api';

// The knowledge-graph Cypher console, in the zone's remote-function dialect (the transport ruling, area 3)
// — same name, same response shape at the call site, transport only. The deleted `api/graph/cypher`
// route existed to keep ONE user-authored query surface enumerated (no blanket write proxy) and
// session-gated; a command has its own endpoint and carries the same gate, so the enumeration is now
// the function's identity rather than a route file.
//
// `command`, not `query`, deliberately: a query is CACHED per argument, so pressing Run on the same
// text would replay the previous result instead of re-executing it. The route was a POST for the same
// reason its comment gives — a user-authored query is the closest thing the read plane has to a write.
//
// The valibot parse MOVES here from the browser (`runGraphCypher` in @rask/media-api parsed the same
// schema after the fetch); none is invented.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

export const runGraphCypher = command(
	v.object({
		query: v.string(),
		/** The console's own ceiling — 200, exactly the client default this replaces. */
		limit: v.optional(v.number(), 200),
		/** The active dataset, or null for the default DB. Read from the DatasetView at the call site:
		 *  the view is a browser-side store and the server has no access to it. */
		dataset: v.nullable(v.string()),
	}),
	async ({ query, limit, dataset }): Promise<ApiResult<GraphCypherResponse>> => {
		const { locals, fetch } = getRequestEvent();
		// The deleted route's `requireSession: true`: fail closed on an auth-enabled stack before the
		// query leaves the zone.
		if (locals.authEnabled && !locals.session) {
			return { ok: false, status: 401, detail: 'sign in required' };
		}
		const suffix = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		let res: Response;
		try {
			res = await fetch(`${VIEWER_API}/api/graph/cypher${suffix}`, {
				method: 'POST',
				headers: { ...bearerHeaders(), 'content-type': 'application/json' },
				body: JSON.stringify({ query, limit }),
			});
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}
		if (!res.ok) {
			let detail = `the viewer answered ${res.status}`;
			try {
				const body: unknown = await res.json();
				if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
			} catch {
				/* a non-JSON error body keeps the status-line detail */
			}
			return { ok: false, status: res.status, detail };
		}
		try {
			return { ok: true, data: parse(GraphCypherResponseSchema, await res.json()) };
		} catch (err) {
			return { ok: false, status: 502, detail: `graph contract drift: ${String(err)}` };
		}
	},
);
