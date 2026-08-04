/**
 * Batch labeling jobs client — the read plane's BULK/auto mode (LabelOp × batch).
 *
 * Framework-agnostic (no Svelte): a thin seam over POST /api/jobs/apply that submits a
 * producer to run over a chunk-level Selection (lasso / scope / corpus). The deriver
 * itself is lance-ns's (lance-ray + the catalog mover) — we only enqueue + poll. Both
 * the read-plane trigger and the annotator's batch `apply()` route through here, so
 * there's ONE submit path. See labeling/types.ts + docs/ACTIVE_LABELING.md.
 */
import { apiUrl } from '@rask/explorer-api/base';

import type { ChunkSelection, Op } from './types';

export interface JobResult {
	job_id: string;
	status: string; // queued | running | succeeded | failed
	backend: string; // "mock" | "remote"
}

export interface BatchJob {
	/** Producer registry key (grounding-dino | insid3 | vlm-judge | embed-propagate | …). */
	producer: string;
	op: Op;
	scope: ChunkSelection;
	prompt?: string;
	dataset?: string;
	/** PROPAGATE (INSID3 / embed-propagate): the few-shot reference — annotation ids of the
	 *  exemplar masks the deriver propagates over `scope` ("1–few exemplars → apply to all"). */
	exemplars?: string[];
}

function scopePayload(s: ChunkSelection): { level: string; keys: string[]; where: string | null } {
	if (s.level === 'chunks') return { level: 'chunks', keys: s.keys, where: null };
	if (s.level === 'scope') return { level: 'scope', keys: [], where: s.where };
	return { level: 'corpus', keys: [], where: null };
}

/** Submit a batch labeling deriver over a chunk-level selection. */
export async function submitBatchJob(job: BatchJob): Promise<JobResult> {
	const res = await fetch(apiUrl('/api/jobs/apply'), {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({
			producer: job.producer,
			op: job.op,
			scope: scopePayload(job.scope),
			prompt: job.prompt ?? null,
			dataset: job.dataset ?? null,
			exemplars: job.exemplars ?? [],
		}),
	});
	if (!res.ok) throw new Error(`job submit failed (HTTP ${res.status})`);
	return (await res.json()) as JobResult;
}

/** Poll a submitted job's status. */
export async function jobStatus(jobId: string): Promise<JobResult> {
	const res = await fetch(apiUrl(`/api/jobs/${encodeURIComponent(jobId)}`));
	if (!res.ok) throw new Error(`job status failed (HTTP ${res.status})`);
	return (await res.json()) as JobResult;
}
