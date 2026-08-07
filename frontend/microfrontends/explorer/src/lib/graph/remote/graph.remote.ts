import { command } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed } from '@rask/api/upstream';
import { sessionGate, viewerJSON } from '$lib/server/doors';
import { GraphCypherResponseSchema, type GraphCypherResponse } from '@rask/explorer-api';

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
// The valibot parse MOVES here from the browser (`runGraphCypher` in @rask/explorer-api parsed the same
// schema after the fetch); none is invented.
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
		// The deleted route's `requireSession: true`: fail closed on an auth-enabled stack before the
		// query leaves the zone.
		const refused = sessionGate();
		if (refused) return refused;
		const suffix = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		return parsed(
			await viewerJSON(`/api/graph/cypher${suffix}`, {
				method: 'POST',
				body: JSON.stringify({ query, limit }),
			}),
			GraphCypherResponseSchema,
			'graph',
		);
	},
);
