/**
 * The CORPUS ROWS behind a page of queue items (#60).
 *
 * A remote `query()` rather than an Arrow `+server.ts`: the estate's transport rule is one per
 * payload KIND, and this is a bounded typed value — one page of the queue, capped server-side at
 * 1000 — not a row batch at scale. The zone's `api/[...path]` viewer proxy could not carry it
 * anyway: it is GET-only by design, and addressing rows by key needs a body.
 *
 * A remote file may export only remote functions, so the join helpers and their contracts live in
 * `../corpus-rows.ts` beside their tests.
 */

import { env } from '$env/dynamic/private';
import { getRequestEvent, query } from '$app/server';
import * as v from 'valibot';

import type { CorpusRow } from '../corpus-rows';

// The viewer service. Matches the zone's own proxy resolution so a page and its rows cannot end up
// pointed at different services on the same stack.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

const RowKeySchema = v.object({
	doc_id: v.string(),
	keys: v.array(v.number()),
});

// NOT exported: a `.remote.ts` may export only remote functions, and the build enforces it.
const ROWS_ARGS = v.object({
	keys: v.array(RowKeySchema),
	dataset: v.nullable(v.string()),
});

interface CorpusRowsResult {
	rows: CorpusRow[];
	/** Which columns form the row identity — carried by the response so a caller holding a task
	 *  never needs to fetch the descriptor just to associate rows back to it. */
	keyFields: string[];
	/** 0 = the viewer was unreachable; the estate's existing signal, read by the other remotes. */
	status: number;
}

/**
 * Fetch the corpus rows for a page of tasks, addressed BY KEY.
 *
 * Failure is a VALUE, never a throw. These rows are DECORATION on a queue that is already usable
 * without them: a rejected fetch across the remote boundary would surface as a page error and take
 * the whole queue down to add a date column. The caller renders what it has and the queue keeps
 * working — which is also why a non-200 returns an empty row list rather than a partial one.
 */
export const fetchCorpusRows = query(
	ROWS_ARGS,
	async ({ keys, dataset }): Promise<CorpusRowsResult> => {
		if (keys.length === 0) return { rows: [], keyFields: [], status: 200 };
		const { fetch, locals } = getRequestEvent();
		const bearer = locals.session?.accessToken;
		const suffix = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';

		let res: Response;
		try {
			res = await fetch(`${VIEWER_API}/api/atlas/chunks/by-key${suffix}`, {
				method: 'POST',
				headers: {
					'content-type': 'application/json',
					...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
				},
				body: JSON.stringify({ keys }),
			});
		} catch {
			return { rows: [], keyFields: [], status: 0 };
		}
		if (!res.ok) return { rows: [], keyFields: [], status: res.status };
		const body = (await res.json()) as { rows?: CorpusRow[]; key_fields?: string[] };
		return { rows: body.rows ?? [], keyFields: body.key_fields ?? [], status: 200 };
	},
);
