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

import { query } from '$app/server';
import * as v from 'valibot';
import { parsed } from '@rask/api/upstream';
import { searchJSON } from '$lib/server/doors';

import { SimilarArgSchema, type SimilarResult } from '../similar-types';

/**
 * The k nearest rows to one the caller already has.
 *
 * Failure is a VALUE, never a throw: a rejected fetch across the remote boundary would skip the
 * caller's honest "the search service is unreachable" branch and surface as a page error. The
 * transport is the zone door over `@rask/api/upstream` (#93): unreachable is `status: 0` (the
 * signal `SimilarPanel` reads), the service's OWN refusal words survive the trip — "no row with
 * that key", "this corpus declares no vector space" are the actionable part — and the endpoint's
 * LIST answer is parsed as one, so an object body cannot silently become an empty neighbour set
 * that reads as "nothing is similar".
 */
export const findSimilar = query(
	SimilarArgSchema,
	async ({ key, dataset, table, space, n }): Promise<SimilarResult> => {
		const params = new URLSearchParams({ key, n: String(n) });
		if (dataset) params.set('dataset', dataset);
		if (table) params.set('table', table);
		if (space) params.set('space', space);
		return parsed(
			await searchJSON(`/api/search/similar?${params}`),
			v.array(v.record(v.string(), v.unknown())),
			'search',
		);
	},
);
