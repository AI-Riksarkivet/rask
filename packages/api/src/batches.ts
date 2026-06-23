// @rask/api/batches — batches, chunks, orchestrator state.

import * as v from 'valibot';
import { parse } from './parse.js';

export const BatchRowSchema = v.object({
	batch_id: v.string(),
	arkiv_referenskod: v.nullable(v.string()),
	arkiv_titel: v.nullable(v.string()),
	volym: v.nullable(v.string()),
	page_count: v.nullable(v.number()),
	iiif_endpoint: v.nullable(v.string()),
	manifest_status: v.nullable(v.string()),
	manifest_error: v.nullable(v.string()),
	cached_pages: v.number(),
	transcribed_pages: v.number(),
	htr_status: v.string(),
	started_at: v.nullable(v.string()),
	finished_at: v.nullable(v.string()),
	last_synced_at: v.nullable(v.string()),
	chunk_id: v.nullable(v.number()),
	chunk_total: v.nullable(v.number()),
	current_rayjob_id: v.nullable(v.string()),
	current_rayjob_submitted_at: v.nullable(v.string()),
});
export type BatchRow = v.InferOutput<typeof BatchRowSchema>;

export const ChunkRowSchema = v.object({
	chunk_id: v.number(),
	chunk_total: v.number(),
	batches: v.number(),
	expected_pages: v.number(),
	cached_pages: v.number(),
	transcribed_pages: v.number(),
	done_batches: v.number(),
});
export type ChunkRow = v.InferOutput<typeof ChunkRowSchema>;

const ListChunksSchema = v.object({ chunks: v.array(ChunkRowSchema) });

export async function listChunks(fetchFn: typeof fetch = fetch): Promise<{ chunks: ChunkRow[] }> {
	const res = await fetchFn('/api/chunks/');
	if (!res.ok) throw new Error(`listChunks: HTTP ${res.status}`);
	return parse(ListChunksSchema, await res.json());
}

const SubmitChunkSchema = v.object({ chunk_id: v.number(), stdout: v.string() });

export async function submitChunk(
	chunkId: number,
	fetchFn: typeof fetch = fetch,
): Promise<{ chunk_id: number; stdout: string }> {
	const res = await fetchFn(`/api/chunks/${chunkId}/submit`, { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`submitChunk(${chunkId}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return parse(SubmitChunkSchema, await res.json());
}

export const BatchesPayloadSchema = v.object({
	generated_at: v.nullable(v.string()),
	summary: v.object({
		total_batches: v.number(),
		accessible: v.object({
			batches: v.number(),
			expected: v.number(),
			cached: v.number(),
			transcribed: v.number(),
		}),
		by_manifest_status: v.record(v.string(), v.number()),
		by_htr_status: v.record(v.string(), v.number()),
	}),
	batches: v.array(BatchRowSchema),
});
export type BatchesPayload = v.InferOutput<typeof BatchesPayloadSchema>;

export async function listBatches(fetchFn: typeof fetch = fetch): Promise<BatchesPayload> {
	const res = await fetchFn('/api/batches/');
	if (!res.ok) throw new Error(`listBatches: HTTP ${res.status}`);
	return parse(BatchesPayloadSchema, await res.json());
}

export async function syncBatches(fetchFn: typeof fetch = fetch): Promise<BatchesPayload> {
	const res = await fetchFn('/api/batches/sync', { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`syncBatches: HTTP ${res.status}: ${body.slice(0, 200)}`);
	}
	return parse(BatchesPayloadSchema, await res.json());
}

// ---------- Ingest (upload + register) ----------

/** Upload image files into images-batch/<id>/ and register the volume. Returns the new batch row. */
export async function uploadVolume(
	id: string,
	files: File[],
	fetchFn: typeof fetch = fetch,
): Promise<BatchRow> {
	const form = new FormData();
	for (const f of files) form.append('files', f, f.name);
	const res = await fetchFn(`/api/batches/${encodeURIComponent(id)}/upload`, {
		method: 'POST',
		body: form,
	});
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`uploadVolume(${id}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return res.json();
}

/** Register an already-uploaded volume prefix (images must already be in the bucket). */
export async function registerVolume(id: string, fetchFn: typeof fetch = fetch): Promise<BatchRow> {
	const res = await fetchFn(`/api/batches/${encodeURIComponent(id)}/register`, { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`registerVolume(${id}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return res.json();
}

// ---------- Orchestrator state ----------
//
// Mirrors the decisions the viewer's orchestrator loop (services/orchestrator_loop.py)
// would make on its next tick: which chunk is in each pipeline slot, what's queued,
// what's in failure-cooldown. Pure derivation from /api/ray/jobs + the batches table.

export const OrchestratorJobSlimSchema = v.object({
	submission_id: v.string(),
	status: v.string(),
	start_time: v.nullable(v.number()),
	chunk_id: v.nullable(v.number()),
});
export type OrchestratorJobSlim = v.InferOutput<typeof OrchestratorJobSlimSchema>;

export const OrchestratorCooldownSchema = v.object({
	submission_id: v.string(),
	chunk_id: v.number(),
	pipeline: v.picklist(['prefetch', 'htr']),
	expires_in_secs: v.number(),
});
export type OrchestratorCooldown = v.InferOutput<typeof OrchestratorCooldownSchema>;

export const StageProgressSchema = v.object({
	stage: v.string(),
	finished: v.number(),
	running: v.number(),
	pending: v.number(),
	failed: v.number(),
	total: v.number(),
});
export type StageProgress = v.InferOutput<typeof StageProgressSchema>;

export const OrchestratorSlotSchema = v.object({
	running: v.nullable(OrchestratorJobSlimSchema),
	next: v.nullable(v.number()),
	queue_len: v.number(),
	stages: v.array(StageProgressSchema),
});
export type OrchestratorSlot = v.InferOutput<typeof OrchestratorSlotSchema>;

export const OrchestratorStateSchema = v.object({
	ok: v.boolean(),
	error: v.optional(v.string()),
	prefetch: v.optional(OrchestratorSlotSchema),
	htr: v.optional(OrchestratorSlotSchema),
	cooldowns: v.optional(v.array(OrchestratorCooldownSchema)),
	ready_threshold: v.optional(v.number()),
	cooldown_secs: v.optional(v.number()),
});
export type OrchestratorState = v.InferOutput<typeof OrchestratorStateSchema>;

export async function rayOrchestrator(fetchFn: typeof fetch = fetch): Promise<OrchestratorState> {
	const res = await fetchFn('/api/orchestrator/state');
	if (!res.ok) throw new Error(`rayOrchestrator: HTTP ${res.status}`);
	return parse(OrchestratorStateSchema, await res.json());
}
