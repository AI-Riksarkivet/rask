/**
 * "More like this" — neighbours of one row, from the SEARCH service.
 *
 * A remote `query()` rather than an Arrow `+server.ts`: the estate's transport rule is one per
 * payload KIND, and this is a bounded typed value (n ≤ 200 hit rows), not a row batch at scale. The
 * explorer's `api/search` rides Arrow because it streams large result sets and takes a multipart
 * image upload; neither applies here, and a byte transport would cost a decoder for 24 rows.
 *
 * A remote file may export only remote functions, so the contracts live in `../similar-types.js`.
 */

import { env } from '$env/dynamic/private';
import { getRequestEvent, query } from '$app/server';
import * as v from 'valibot';

import { SimilarArgSchema, type SimilarResult } from '../similar-types';

// The search service. Matches the explorer's own resolution (`SEARCH_API ?? :8102`) so the two
// zones cannot end up pointed at different services on the same stack.
const SEARCH_API = env.SEARCH_API ?? 'http://localhost:8102';

/**
 * The k nearest rows to one the caller already has.
 *
 * Failure is a VALUE, never a throw: a rejected fetch across the remote boundary would skip the
 * caller's honest "the search service is unreachable" branch and surface as a page error. `status:
 * 0` is the estate's existing signal for unreachable, which the other remote modules already read.
 */
export const findSimilar = query(
	SimilarArgSchema,
	async ({ key, dataset, table, space, n }): Promise<SimilarResult> => {
		const { fetch, locals } = getRequestEvent();
		const bearer = locals.session?.accessToken;

		const params = new URLSearchParams({ key, n: String(n) });
		if (dataset) params.set('dataset', dataset);
		if (table) params.set('table', table);
		if (space) params.set('space', space);

		let res: Response;
		try {
			res = await fetch(`${SEARCH_API}/api/search/similar?${params}`, {
				headers: bearer ? { authorization: `Bearer ${bearer}` } : {},
			});
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}

		const body: unknown = await res.json().catch(() => null);
		if (!res.ok) {
			// The service's OWN words. Its refusals are the actionable part — "no row with that key",
			// "this corpus declares no vector space", "key has 2 parts, expected 3" — and collapsing
			// them into "search failed" throws away the only thing that tells someone what to do.
			const detail =
				body && typeof body === 'object' && typeof (body as { detail?: unknown }).detail === 'string'
					? (body as { detail: string }).detail
					: `HTTP ${res.status}`;
			return { ok: false, status: res.status, detail };
		}

		// The endpoint answers a LIST. `annotatorJSON`'s object cast would have turned that into `{}`
		// silently — an empty neighbour set that reads as "nothing is similar".
		const parsed = v.safeParse(v.array(v.record(v.string(), v.unknown())), body);
		if (!parsed.success) {
			return { ok: false, status: 0, detail: `contract drift: ${parsed.issues[0]?.message ?? 'decode failed'}` };
		}
		return { ok: true, data: parsed.output };
	},
);
