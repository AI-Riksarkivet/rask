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
	job_id: string | null;
	status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'STOPPED' | string;
	entrypoint: string | null;
	batches: string[];
	start_time: number | null;
	end_time: number | null;
	message: string | null;
	error_type: string | null;
	driver_exit_code: number | null;
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
	uuid: string | null;
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

export interface ActorInfo {
	actor_id: string | null;
	class_name: string;
	name: string | null;
	repr_name: string | null;
	state: string;
	pid: number | null;
	node_id: string | null;
	job_id: string | null;
	ray_namespace: string | null;
	num_restarts: number;
	is_detached: boolean;
	placement_group_id: string | null;
	required_resources: Record<string, number>;
	death_reason: string | null;
	// Live telemetry (from /logical/actors; null when unavailable).
	start_time_ms: number | null;
	end_time_ms: number | null;
	cpu_percent: number | null;
	rss_bytes: number | null;
	num_fds: number | null;
	gpu_util: number | null;
	gpu_mem_mb: number | null;
	num_executed_tasks: number | null;
	num_running_tasks: number | null;
	num_pending_tasks: number | null;
	task_queue_length: number | null;
	ip_address: string | null;
	worker_id: string | null;
}

export interface ActorsPayload {
	ok: boolean;
	dashboard_url: string;
	actors: ActorInfo[];
	error?: string | null;
}

/** Ray actors, merged from the state API + /logical/actors by the viewer backend. */
export async function actorsList(): Promise<ActorInfo[]> {
	const res = await fetch('/api/ray/actors');
	if (!res.ok) throw new Error(`actorsList: HTTP ${res.status}`);
	const payload: ActorsPayload = await res.json();
	if (!payload.ok) throw new Error(payload.error ?? 'actors unavailable');
	return payload.actors ?? [];
}

export interface TaskInfo {
	task_id: string | null;
	name: string | null;
	func_or_class_name: string | null;
	type: string | null;
	state: string;
	job_id: string | null;
	actor_id: string | null;
	node_id: string | null;
	worker_pid: number | null;
	attempt_number: number | null;
	error_type: string | null;
	error_message: string | null;
	required_resources: Record<string, number>;
	creation_time_ms: number | null;
	start_time_ms: number | null;
	end_time_ms: number | null;
}

export interface TasksPayload {
	ok: boolean;
	dashboard_url: string;
	tasks: TaskInfo[];
	error?: string | null;
}

export async function tasksList(): Promise<TaskInfo[]> {
	const res = await fetch('/api/ray/tasks');
	if (!res.ok) throw new Error(`tasksList: HTTP ${res.status}`);
	const payload: TasksPayload = await res.json();
	if (!payload.ok) throw new Error(payload.error ?? 'tasks unavailable');
	return payload.tasks ?? [];
}

export interface RayEvent {
	event_id: string | null;
	severity: string;
	message: string;
	time: string | null;
	source_type: string | null;
}

export interface OverviewPayload {
	ok: boolean;
	dashboard_url: string;
	ray_version: string | null;
	session_name: string | null;
	events: RayEvent[];
	error?: string | null;
}

export async function rayOverview(): Promise<OverviewPayload> {
	const res = await fetch('/api/ray/overview');
	if (!res.ok) throw new Error(`rayOverview: HTTP ${res.status}`);
	return res.json();
}

export interface JobLogsPayload {
	ok: boolean;
	submission_id: string;
	logs: string;
	error?: string | null;
}

export async function rayJobLogs(submissionId: string, tail = 2000): Promise<JobLogsPayload> {
	const res = await fetch(`/api/ray/jobs/${encodeURIComponent(submissionId)}/logs?tail=${tail}`);
	if (!res.ok) throw new Error(`rayJobLogs: HTTP ${res.status}`);
	return res.json();
}

export interface LogsPayload {
	ok: boolean;
	node_id: string | null;
	filename: string | null;
	files: Record<string, string[]>;
	text: string | null;
	error?: string | null;
}

export async function rayLogFiles(nodeId: string): Promise<LogsPayload> {
	const res = await fetch(`/api/ray/logs?node_id=${encodeURIComponent(nodeId)}`);
	if (!res.ok) throw new Error(`rayLogFiles: HTTP ${res.status}`);
	return res.json();
}

export async function rayLogContent(
	nodeId: string,
	filename: string,
	lines = 500
): Promise<LogsPayload> {
	const qs = new URLSearchParams({ node_id: nodeId, filename, lines: String(lines) });
	const res = await fetch(`/api/ray/logs?${qs}`);
	if (!res.ok) throw new Error(`rayLogContent: HTTP ${res.status}`);
	return res.json();
}

export interface ServeReplica {
	replica_id: string;
	state: string;
	node_id?: string;
	node_ip?: string;
	node_instance_id?: string;
	actor_name?: string;
	pid?: number;
	start_time_s?: number;
	log_file_path?: string;
}

export interface ServeRuntimeEnv {
	uv?: string[];
	pip?: string[];
	working_dir?: string;
	env_vars?: Record<string, string>;
}

export interface ServeRayActorOptions {
	num_cpus?: number;
	num_gpus?: number;
	resources?: Record<string, number>;
	runtime_env?: ServeRuntimeEnv;
}

export interface ServeDeploymentConfig {
	num_replicas?: number;
	max_ongoing_requests?: number;
	max_queued_requests?: number;
	health_check_period_s?: number;
	health_check_timeout_s?: number;
	graceful_shutdown_wait_loop_s?: number;
	graceful_shutdown_timeout_s?: number;
	rolling_update_percentage?: number;
	autoscaling_config?: Record<string, unknown> | null;
	user_config?: unknown;
	ray_actor_options?: ServeRayActorOptions;
	request_router_config?: { request_router_class?: string };
}

export interface ServeDeployment {
	name: string;
	status: string;
	status_trigger?: string;
	message?: string;
	target_num_replicas?: number;
	required_resources?: Record<string, number>;
	replicas: ServeReplica[];
	recent_dead_replicas?: ServeReplica[];
	deployment_config?: ServeDeploymentConfig;
}

export interface ServeTopologyNode {
	name: string;
	is_ingress: boolean;
	outbound_deployments: { name: string }[];
}

export interface ServeTopology {
	app_name: string;
	nodes: Record<string, ServeTopologyNode>;
	ingress_deployment?: string;
}

export interface ServeApplication {
	name: string;
	route_prefix: string | null;
	docs_path: string | null;
	status: string;
	message?: string;
	source?: string;
	external_scaler_enabled?: boolean;
	last_deployed_time_s?: number;
	deployments: Record<string, ServeDeployment>;
	deployment_topology?: ServeTopology;
}

export interface ServeProxy {
	status?: string;
	node_id?: string;
	node_ip?: string;
	node_instance_id?: string;
	log_file_path?: string;
}

export interface ServeControllerInfo {
	node_ip?: string;
	node_instance_id?: string;
	log_file_path?: string;
}

export interface ServeControllerHealth {
	uptime_s?: number;
	num_control_loops?: number;
	loops_per_second?: number;
	num_asyncio_tasks?: number;
}

export interface ServeTargetGroup {
	targets: { ip: string; port: number; instance_id?: string; name?: string }[];
}

/** Raw Ray Serve status, proxied straight from the dashboard's /api/serve/applications/. */
export interface ServePayload {
	applications: Record<string, ServeApplication>;
	proxies?: Record<string, ServeProxy>;
	proxy_location?: string;
	http_options?: { host?: string; port?: number };
	grpc_options?: { port?: number };
	target_capacity?: number | null;
	controller_info?: ServeControllerInfo;
	controller_health_metrics?: ServeControllerHealth;
	target_groups?: ServeTargetGroup[];
}

export async function serveApplications(): Promise<ServePayload> {
	const res = await fetch('/api/serve/applications/');
	if (!res.ok) throw new Error(`serveApplications: HTTP ${res.status}`);
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
