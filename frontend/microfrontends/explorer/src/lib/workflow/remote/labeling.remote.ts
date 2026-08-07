import { command } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { parsed } from '@rask/api/upstream';
import { annotatorJSON, sessionGate } from '$lib/server/doors';
import type { SaveResult } from '@rask/labeling/tag-writer';
import type { JobResult } from '@rask/labeling/jobs';

// The two writes this zone makes into the ANNOTATOR service, in the zone's remote-function dialect
// (the transport ruling, area 3) — same names, same payloads on the wire, transport only. Both deleted
// routes (`api/annotations/tags`, `api/jobs/apply`) were the same copy-pasted bearer-forward template
// with `requireSession: true`; that gate is preserved verbatim below, and it is the only thing those
// files did beyond forwarding.
//
// They live in ONE module because they are one plane: media's workflow promotes its chunk tags to real
// annotation rows, and enqueues a producer over a chunk selection. Everything else on
// `/api/annotations/**` and `/api/assist/**` belongs to the annotator zone and is not reachable from
// here — which is what deleting the routes rather than widening them keeps true.
//
// Transport and gate live in `$lib/server/doors` over `@rask/api/upstream` (#93). The answers are
// PARSED (#94): the schemas are typed against the wire contracts imported from `@rask/labeling`, so
// a drift between this schema and the shared contract is a compile error, and a drifted wire answer
// is a named 502 the caller renders — never a silently-wrong count.

const SaveResultSchema: v.GenericSchema<unknown, SaveResult> = v.looseObject({
	saved: v.number(),
	version: v.number(),
});

const JobResultSchema: v.GenericSchema<unknown, JobResult> = v.looseObject({
	job_id: v.string(),
	status: v.string(),
	backend: v.string(),
});

/** One media-plane POST, parsed on the way out. */
const annotatorPost = <T>(
	path: string,
	body: unknown,
	schema: v.GenericSchema<unknown, T>,
): Promise<ApiResult<T>> =>
	annotatorJSON(path, { method: 'POST', body: JSON.stringify(body) }).then((r) =>
		parsed(r, schema, 'annotator'),
	);

/** One tagged unit: the doc key plus the NON-doc identity fields, positional (pairs with the
 *  descriptor's `keyFields` minus the doc key) — the arity-generic shape `@rask/labeling/tag-writer`
 *  builds and this zone has always sent. */
const TagWriteSchema = v.object({
	doc_id: v.string(),
	keys: v.array(v.number()),
	labels: v.array(v.string()),
});

/** Persist the run's chunk tags as annotation ROWS — one merge_insert version server-side, idempotent
 *  by deterministic id. Adds and removes travel together so an un-tag is one version too. */
export const saveTagsAsAnnotations = command(
	v.object({
		adds: v.array(TagWriteSchema),
		removes: v.optional(v.array(TagWriteSchema)),
		base_version: v.optional(v.nullable(v.number())),
		/** The active dataset, or null for the default DB — read from the DatasetView at the call site
		 *  (a browser-side store the server cannot see). */
		dataset: v.nullable(v.string()),
	}),
	async ({ adds, removes, base_version, dataset }): Promise<ApiResult<SaveResult>> => {
		const refused = sessionGate();
		if (refused) return refused;
		const suffix = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		return annotatorPost(
			`/api/annotations/tags${suffix}`,
			{
				adds,
				removes,
				base_version,
			},
			SaveResultSchema,
		);
	},
);

/** The chunk-level selection a batch job runs over. */
const ChunkSelectionSchema = v.variant('level', [
	v.object({ level: v.literal('chunks'), keys: v.array(v.string()) }),
	v.object({ level: v.literal('scope'), where: v.string() }),
	v.object({ level: v.literal('corpus') }),
]);

/** The wire shape the annotator's `/api/jobs/apply` takes — flattened from the selection union exactly
 *  as `scopePayload` in `@rask/labeling/jobs` did, so the request body is byte-for-byte what it was. */
function scopePayload(scope: v.InferOutput<typeof ChunkSelectionSchema>): {
	level: string;
	keys: string[];
	where: string | null;
} {
	if (scope.level === 'chunks') return { level: 'chunks', keys: scope.keys, where: null };
	if (scope.level === 'scope') return { level: 'scope', keys: [], where: scope.where };
	return { level: 'corpus', keys: [], where: null };
}

/** Submit a batch labeling deriver over a chunk-level selection. We only ENQUEUE — the deriver runs
 *  async and its predictions surface on re-read; nothing here polls (the status passthrough this zone
 *  used to carry had no caller at all and was deleted with these routes). */
export const submitBatchJob = command(
	v.object({
		producer: v.string(),
		op: v.picklist(['set', 'verdict', 'predict', 'propagate', 'judge']),
		scope: ChunkSelectionSchema,
		prompt: v.optional(v.nullable(v.string())),
		dataset: v.optional(v.nullable(v.string())),
		exemplars: v.optional(v.array(v.string())),
	}),
	async ({ producer, op, scope, prompt, dataset, exemplars }): Promise<ApiResult<JobResult>> => {
		const refused = sessionGate();
		if (refused) return refused;
		return annotatorPost(
			'/api/jobs/apply',
			{
				producer,
				op,
				scope: scopePayload(scope),
				prompt: prompt ?? null,
				dataset: dataset ?? null,
				exemplars: exemplars ?? [],
			},
			JobResultSchema,
		);
	},
);
