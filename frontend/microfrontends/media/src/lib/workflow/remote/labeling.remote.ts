import { command, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { SaveResult } from '@rask/labeling/tag-writer';
import type { JobResult } from '@rask/labeling/jobs';

// The two writes this zone makes into the ANNOTATOR service, in the zone's remote-function dialect
// (open_transport.md, area 3) — same names, same payloads on the wire, transport only. Both deleted
// routes (`api/annotations/tags`, `api/jobs/apply`) were the same copy-pasted bearer-forward template
// with `requireSession: true`; that gate is preserved verbatim below, and it is the only thing those
// files did beyond forwarding.
//
// They live in ONE module because they are one plane: media's workflow promotes its chunk tags to real
// annotation rows, and enqueues a producer over a chunk selection. Everything else on
// `/api/annotations/**` and `/api/assist/**` belongs to the annotator zone and is not reachable from
// here — which is what deleting the routes rather than widening them keeps true.
//
// Neither surface was parsed before (both clients cast the JSON), so neither is parsed now: moving a
// transport is not the moment to invent a contract.
const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** `requireSession: true`, unchanged: a write must be attributable to a real user, and on an
 *  auth-enabled stack an anonymous caller is refused before the request leaves the zone. */
function sessionGate(): { ok: false; status: number; detail: string } | null {
	const { locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in required' };
	}
	return null;
}

async function annotatorJSON<T>(path: string, body: unknown): Promise<ApiResult<T>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${ANNOTATOR_API}${path}`, {
			method: 'POST',
			headers: { ...bearerHeaders(), 'content-type': 'application/json' },
			body: JSON.stringify(body),
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `the annotator answered ${res.status}`;
		try {
			const problem: unknown = await res.json();
			if (problem && typeof problem === 'object' && 'detail' in problem) {
				detail = String(problem.detail);
			}
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	return { ok: true, data: (await res.json()) as T };
}

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
		return annotatorJSON<SaveResult>(`/api/annotations/tags${suffix}`, {
			adds,
			removes,
			base_version,
		});
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
		return annotatorJSON<JobResult>('/api/jobs/apply', {
			producer,
			op,
			scope: scopePayload(scope),
			prompt: prompt ?? null,
			dataset: dataset ?? null,
			exemplars: exemplars ?? [],
		});
	},
);
