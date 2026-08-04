import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { JobResult } from '@rask/labeling/jobs';

// Batch labeling jobs — submit a producer over a chunk-level selection, then poll it
// (the transport ruling, area 4). Both `/api/jobs/apply` and the `/api/jobs/[...path]` status proxy are
// deleted; these two functions are what they carried.
//
// SHARED-SEAM NOTE: `@rask/labeling/jobs` still owns this wire shape for the MEDIA zone, which mounts
// its own `/api/jobs/apply` route and reaches it through the shared browser client. That package is
// framework-agnostic by contract (it never imports `$app/*`), so it cannot host a remote function;
// this zone's copy is the annotator's transport, and the `JobResult` contract stays imported from
// there rather than re-declared — one wire type, two transports.
//
// `requireSession: true` carried over from the deleted apply route: enqueuing a deriver over a corpus
// selection is a write, and a write must be attributable.

const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

async function annotatorJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	let res: Response;
	try {
		res = await fetch(`${ANNOTATOR_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
	if (!res.ok) {
		return {
			ok: false,
			status: res.status,
			detail: typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`,
		};
	}
	return { ok: true, data: body };
}

/** The chunk-level selection the deriver runs over — the three shapes `ChunkSelection` takes. */
const ScopeSchema = v.variant('level', [
	v.object({ level: v.literal('chunks'), keys: v.array(v.string()) }),
	v.object({ level: v.literal('scope'), where: v.string() }),
	v.object({ level: v.literal('corpus') }),
]);

/** The wire scope (`{level, keys, where}` — always all three keys), mirroring the flattening the
 *  shared client does. Kept here rather than imported because `@rask/labeling/jobs` keeps it private
 *  to its own fetch. */
function scopePayload(s: v.InferOutput<typeof ScopeSchema>): {
	level: string;
	keys: string[];
	where: string | null;
} {
	if (s.level === 'chunks') return { level: 'chunks', keys: s.keys, where: null };
	if (s.level === 'scope') return { level: 'scope', keys: [], where: s.where };
	return { level: 'corpus', keys: [], where: null };
}

/** Submit a batch labeling deriver over a chunk-level selection. */
export const submitBatchJob = command(
	v.object({
		/** Producer registry key (grounding-dino | insid3 | vlm-judge | embed-propagate | …). */
		producer: v.string(),
		op: v.picklist(['set', 'verdict', 'predict', 'propagate', 'judge']),
		scope: ScopeSchema,
		prompt: v.optional(v.string()),
		dataset: v.optional(v.string()),
		/** PROPAGATE (INSID3 / embed-propagate): annotation ids of the exemplar masks the deriver
		 *  propagates over `scope` ("1–few exemplars → apply to all"). */
		exemplars: v.optional(v.array(v.string())),
	}),
	async ({ producer, op, scope, prompt, dataset, exemplars }): Promise<ApiResult<JobResult>> => {
		if (signedOut()) return { ok: false, status: 401, detail: 'sign in required' };
		const result = await annotatorJSON('/api/jobs/apply', {
			method: 'POST',
			body: JSON.stringify({
				producer,
				op,
				scope: scopePayload(scope),
				prompt: prompt ?? null,
				dataset: dataset ?? null,
				exemplars: exemplars ?? [],
			}),
		});
		return result.ok ? { ok: true, data: result.data as JobResult } : result;
	},
);

/** Poll a submitted job's status. A plain `query()` for now: job events are not surfaced to the web
 *  plane, so there is no change signal to make this `query.live` — a poller refreshes it when one
 *  is wired (the submit path is fire-and-forget with a toast today). */
export const jobStatus = query(
	v.object({ jobId: v.string() }),
	async ({ jobId }): Promise<ApiResult<JobResult>> => {
		const result = await annotatorJSON(`/api/jobs/${encodeURIComponent(jobId)}`);
		return result.ok ? { ok: true, data: result.data as JobResult } : result;
	},
);
