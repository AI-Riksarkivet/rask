/**
 * Wire contracts for "more like this".
 *
 * Beside the remote module rather than inside it, because a `.remote.ts` may export ONLY remote
 * functions — a schema declared there is unreachable to a test, and this zone has already been bitten
 * by a wire schema nobody could exercise (`dataset_version`, silently stripped for weeks).
 */

import type { ApiResult } from '@rask/api/client';
import * as v from 'valibot';

import { MAX_SIMILAR_N } from './similar-limits';

/** One "more like this" request. Mirrors the service's `SimilarSpec`. */
export const SimilarArgSchema = v.object({
	/** The seed row, its identity fields joined by `/`. */
	key: v.pipe(v.string(), v.minLength(1)),
	/** The corpus. Absent means the service's default DB. */
	dataset: v.optional(v.nullable(v.string())),
	/** Which of the corpus's searchable tables. Absent means its default. */
	table: v.optional(v.nullable(v.string())),
	/** Which declared vector space (`semantic` / `visual` / …). Absent means the corpus's first. */
	space: v.optional(v.nullable(v.string())),
	/** Capped to the SERVICE's own bound, so an over-large ask is refused here rather than after a
	 *  round trip — the same disagreement that made the send cap a delay before a refusal. */
	n: v.optional(v.pipe(v.number(), v.integer(), v.minValue(1), v.maxValue(MAX_SIMILAR_N)), 24),
});
export type SimilarArg = v.InferInput<typeof SimilarArgSchema>;

/** The neighbours, or why there are none. */
export type SimilarResult = ApiResult<Record<string, unknown>[]>;
