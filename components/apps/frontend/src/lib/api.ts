import type { PageEntry } from './types';

/** API client for the ra-viewer backend. Same-origin in production; proxied via vite in dev. */

export async function listPages(volume: string): Promise<PageEntry[]> {
	const res = await fetch(`/api/volumes/${encodeURIComponent(volume)}/pages`);
	if (!res.ok) throw new Error(`listPages(${volume}): HTTP ${res.status}`);
	return res.json();
}

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
	const res = await fetch('/api/chunks');
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

// ---------- Ray dashboard proxy ----------
//
// Endpoints intentionally never 5xx — when the dashboard is unreachable we
// surface { ok: false, error } so the UI can render an "offline" badge.

export interface RayHealth {
	ok: boolean;
	dashboard_url: string;
	ray_version?: string;
	error?: string;
}

export interface RayJob {
	submission_id: string;
	status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'STOPPED' | string;
	entrypoint: string | null;
	batches: string[];
	start_time: number | null;
	end_time: number | null;
	message: string | null;
	error_type: string | null;
	logs_url: string | null;
	metadata: Record<string, string>;
}

export interface RayJobsPayload {
	ok: boolean;
	dashboard_url: string;
	jobs?: RayJob[];
	error?: string;
}

export interface RayClusterPayload {
	ok: boolean;
	dashboard_url: string;
	node_count?: number;
	alive_count?: number;
	total_resources?: { CPU: number; GPU: number; memory: number };
	used_resources?: { CPU: number; GPU: number; memory: number };
	nodes?: RayNode[];
	error?: string;
}

export interface RayGpu {
	index: number | null;
	name: string | null;
	utilization_percent: number | null;
	memory_used_mb: number | null;
	memory_total_mb: number | null;
	temperature_c: number | null;
}

export interface RayNode {
	node_id: string | null;
	node_ip: string | null;
	hostname: string | null;
	node_type: string | null;
	is_head: boolean;
	alive: boolean;
	resources_total: Record<string, number>;
	resources_used: Record<string, number>;
	host_cpu_percent: number | null;
	host_mem_total: number | null;
	host_mem_used: number | null;
	gpus: RayGpu[];
}

export async function rayHealth(): Promise<RayHealth> {
	const res = await fetch('/api/ray/health');
	if (!res.ok) throw new Error(`rayHealth: HTTP ${res.status}`);
	return res.json();
}

export async function rayJobs(): Promise<RayJobsPayload> {
	const res = await fetch('/api/ray/jobs');
	if (!res.ok) throw new Error(`rayJobs: HTTP ${res.status}`);
	return res.json();
}

export async function rayCluster(): Promise<RayClusterPayload> {
	const res = await fetch('/api/ray/cluster');
	if (!res.ok) throw new Error(`rayCluster: HTTP ${res.status}`);
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
	const res = await fetch('/api/batches');
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

export interface SearchHit {
	batch_id: string;
	page_id: string;
	page_idx: number;
	line_id: string;
	line_idx: number;
	text: string;
	confidence: number;
	hpos: number;
	vpos: number;
	width: number;
	height: number;
	polygon: string;
	thumb_key: string;
	thumb_url: string | null;
	/** Parent volume's EAD row, or null when the batch isn't in the harvested
	 *  catalog. Backend joins by batch_id == bild_id at search time. The
	 *  embedded row doesn't carry the listed/cached/transcribed flags — we
	 *  already know it's transcribed (otherwise it wouldn't be in the line
	 *  index in the first place). */
	catalog: CatalogHit | null;
}

export interface SearchResponse {
	ok: boolean;
	query: string;
	count: number;
	hits: SearchHit[];
}

export interface SearchStats {
	available: boolean;
	rows: number;
}

export async function searchLines(query: string, limit = 50): Promise<SearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/search?${params}`);
	if (!res.ok) throw new Error(`searchLines: HTTP ${res.status}`);
	return res.json();
}

export async function searchStats(): Promise<SearchStats> {
	const res = await fetch('/api/search/stats');
	if (!res.ok) throw new Error(`searchStats: HTTP ${res.status}`);
	return res.json();
}

export interface CatalogHit {
	id: string;
	reference_code: string;
	archive_code: string;
	fonds_id: string;
	fonds_title: string;
	series_id: string;
	series_title: string;
	volume_id: string;
	volume_title: string;
	date_text: string;
	date_start: number | null;
	date_end: number | null;
	description: string;
	bild_id: string;
	bildvisning_url: string;
	iiif_manifest: string;
	thumbnail_url: string;
	/** Three nested local-state flags (transcribed ⊂ cached ⊂ listed):
	 *  - listed:      bild_id is in batches.db (we know the manifest).
	 *  - cached:      cached_pages > 0 (at least one image downloaded).
	 *  - transcribed: transcribed_pages > 0 (at least one ALTO produced).
	 *  Used to pick the click target (our viewer when cached, Riksarkivet
	 *  otherwise) and to render escalating progress badges. */
	listed: boolean;
	cached: boolean;
	transcribed: boolean;
}

export interface CatalogSearchResponse {
	ok: boolean;
	query: string;
	count: number;
	hits: CatalogHit[];
}

export async function searchCatalog(query: string, limit = 50): Promise<CatalogSearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/catalog/search?${params}`);
	if (!res.ok) throw new Error(`searchCatalog: HTTP ${res.status}`);
	return res.json();
}

export async function catalogStats(): Promise<SearchStats> {
	const res = await fetch('/api/catalog/search/stats');
	if (!res.ok) throw new Error(`catalogStats: HTTP ${res.status}`);
	return res.json();
}

export type CatalogTier = 'listed' | 'cached' | 'transcribed';

export interface CatalogBrowseResponse {
	ok: boolean;
	tier: CatalogTier;
	count: number;
	total: number;
	offset: number;
	hits: CatalogHit[];
}

/** Browse mode — list batches at >= `tier` from batches.db, enriched with
 *  EAD catalog metadata. Returns at most `limit` rows (server-capped at 2000)
 *  starting at `offset`. Used by the /browse page. */
export async function browseCatalog(
	tier: CatalogTier = 'cached',
	limit = 500,
	offset = 0,
): Promise<CatalogBrowseResponse> {
	const params = new URLSearchParams({ tier, limit: String(limit), offset: String(offset) });
	const res = await fetch(`/api/catalog/browse?${params}`);
	if (!res.ok) throw new Error(`browseCatalog: HTTP ${res.status}`);
	return res.json();
}

/** Direct catalog lookup by batch_id (== bild_id). Returns null when the
 *  batch isn't in the harvested EAD catalog (e.g. test batches), so the
 *  viewer can render conditionally without a separate error path. */
export async function getBatchCatalog(batchId: string): Promise<CatalogHit | null> {
	const res = await fetch(`/api/batches/${encodeURIComponent(batchId)}/catalog`);
	if (res.status === 404) return null;
	if (!res.ok) throw new Error(`getBatchCatalog: HTTP ${res.status}`);
	// The endpoint returns just the row, not the {listed, cached, transcribed}
	// flags — those are search-time enrichment. Fill them in as false so the
	// type matches; consumers reading them will see "no local state known".
	const row = await res.json();
	return { listed: false, cached: false, transcribed: false, ...row };
}

export async function rayOrchestrator(): Promise<OrchestratorState> {
	const res = await fetch('/api/orchestrator/state');
	if (!res.ok) throw new Error(`rayOrchestrator: HTTP ${res.status}`);
	return res.json();
}

export function imageUrl(volume: string, key: string): string {
	return `/api/volumes/${encodeURIComponent(volume)}/pages/${encodeURIComponent(key)}/image`;
}

export function altoUrl(volume: string, key: string): string {
	return `/api/volumes/${encodeURIComponent(volume)}/pages/${encodeURIComponent(key)}/alto`;
}

export async function fetchAlto(volume: string, key: string): Promise<string | null> {
	const res = await fetch(altoUrl(volume, key));
	if (res.status === 404) return null;
	if (!res.ok) throw new Error(`fetchAlto: HTTP ${res.status}`);
	return res.text();
}
