// @rask/api/batches — batches, chunks, orchestrator state.

export interface BatchRow {
	batch_id: string;
	arkiv_referenskod: string | null;
	arkiv_titel: string | null;
	volym: string | null;
	page_count: number | null;
	iiif_endpoint: string | null;
	manifest_status: string | null;
	manifest_error: string | null;
	cached_pages: number;
	transcribed_pages: number;
	htr_status: string;
	started_at: string | null;
	finished_at: string | null;
	last_synced_at: string | null;
	chunk_id: number | null;
	chunk_total: number | null;
	current_rayjob_id: string | null;
	current_rayjob_submitted_at: string | null;
}

export interface ChunkRow {
	chunk_id: number;
	chunk_total: number;
	batches: number;
	expected_pages: number;
	cached_pages: number;
	transcribed_pages: number;
	done_batches: number;
}

export async function listChunks(): Promise<{ chunks: ChunkRow[] }> {
	const res = await fetch('/api/chunks/');
	if (!res.ok) throw new Error(`listChunks: HTTP ${res.status}`);
	return res.json();
}

export async function submitChunk(chunkId: number): Promise<{ chunk_id: number; stdout: string }> {
	const res = await fetch(`/api/chunks/${chunkId}/submit`, { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`submitChunk(${chunkId}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return res.json();
}

export interface BatchesPayload {
	generated_at: string | null;
	summary: {
		total_batches: number;
		accessible: { batches: number; expected: number; cached: number; transcribed: number };
		by_manifest_status: Record<string, number>;
		by_htr_status: Record<string, number>;
	};
	batches: BatchRow[];
}

export async function listBatches(): Promise<BatchesPayload> {
	const res = await fetch('/api/batches/');
	if (!res.ok) throw new Error(`listBatches: HTTP ${res.status}`);
	return res.json();
}

export async function syncBatches(): Promise<BatchesPayload> {
	const res = await fetch('/api/batches/sync', { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`syncBatches: HTTP ${res.status}: ${body.slice(0, 200)}`);
	}
	return res.json();
}

// ---------- Orchestrator state ----------
//
// Mirrors the decisions the viewer's orchestrator loop (services/orchestrator_loop.py)
// would make on its next tick: which chunk is in each pipeline slot, what's queued,
// what's in failure-cooldown. Pure derivation from /api/ray/jobs + the batches table.

export interface OrchestratorJobSlim {
	submission_id: string;
	status: string;
	start_time: number | null;
	chunk_id: number | null;
}

export interface OrchestratorCooldown {
	submission_id: string;
	chunk_id: number;
	pipeline: 'prefetch' | 'htr';
	expires_in_secs: number;
}

export interface StageProgress {
	stage: string;
	finished: number;
	running: number;
	pending: number;
	failed: number;
	total: number;
}

export interface OrchestratorSlot {
	running: OrchestratorJobSlim | null;
	next: number | null;
	queue_len: number;
	stages: StageProgress[];
}

export interface OrchestratorState {
	ok: boolean;
	error?: string;
	prefetch?: OrchestratorSlot;
	htr?: OrchestratorSlot;
	cooldowns?: OrchestratorCooldown[];
	ready_threshold?: number;
	cooldown_secs?: number;
}

export async function rayOrchestrator(): Promise<OrchestratorState> {
	const res = await fetch('/api/orchestrator/state');
	if (!res.ok) throw new Error(`rayOrchestrator: HTTP ${res.status}`);
	return res.json();
}
