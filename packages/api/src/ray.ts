// @rask/api/ray — Ray dashboard + Serve introspection (compute domain).

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
	lines = 500,
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
