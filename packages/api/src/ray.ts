// @rask/api/ray — Ray dashboard + Serve introspection (compute domain).

import * as v from 'valibot';
import { parse } from './parse.js';

// ---------- Ray dashboard proxy ----------
//
// Endpoints intentionally never 5xx — when the dashboard is unreachable we
// surface { ok: false, error } so the UI can render an "offline" badge.

export const RayHealthSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	ray_version: v.optional(v.string()),
	error: v.optional(v.nullable(v.string())),
});
export type RayHealth = v.InferOutput<typeof RayHealthSchema>;

export const RayJobSchema = v.object({
	submission_id: v.string(),
	job_id: v.nullable(v.string()),
	// Literal union that also allows arbitrary strings → just v.string().
	status: v.string(),
	entrypoint: v.nullable(v.string()),
	batches: v.array(v.string()),
	start_time: v.nullable(v.number()),
	end_time: v.nullable(v.number()),
	message: v.nullable(v.string()),
	error_type: v.nullable(v.string()),
	driver_exit_code: v.nullable(v.number()),
	logs_url: v.nullable(v.string()),
	metadata: v.record(v.string(), v.string()),
});
export type RayJob = v.InferOutput<typeof RayJobSchema>;

export const RayJobsPayloadSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	jobs: v.optional(v.array(RayJobSchema)),
	error: v.optional(v.nullable(v.string())),
});
export type RayJobsPayload = v.InferOutput<typeof RayJobsPayloadSchema>;

const ResourceTripletSchema = v.object({
	CPU: v.number(),
	GPU: v.number(),
	memory: v.number(),
});

export const RayGpuSchema = v.object({
	index: v.nullable(v.number()),
	uuid: v.nullable(v.string()),
	name: v.nullable(v.string()),
	utilization_percent: v.nullable(v.number()),
	memory_used_mb: v.nullable(v.number()),
	memory_total_mb: v.nullable(v.number()),
	temperature_c: v.nullable(v.number()),
});
export type RayGpu = v.InferOutput<typeof RayGpuSchema>;

export const RayNodeSchema = v.object({
	node_id: v.nullable(v.string()),
	node_ip: v.nullable(v.string()),
	hostname: v.nullable(v.string()),
	node_type: v.nullable(v.string()),
	is_head: v.boolean(),
	alive: v.boolean(),
	resources_total: v.record(v.string(), v.number()),
	resources_used: v.record(v.string(), v.number()),
	host_cpu_percent: v.nullable(v.number()),
	host_mem_total: v.nullable(v.number()),
	host_mem_used: v.nullable(v.number()),
	gpus: v.array(RayGpuSchema),
});
export type RayNode = v.InferOutput<typeof RayNodeSchema>;

export const RayClusterPayloadSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	node_count: v.optional(v.number()),
	alive_count: v.optional(v.number()),
	total_resources: v.optional(ResourceTripletSchema),
	used_resources: v.optional(ResourceTripletSchema),
	nodes: v.optional(v.array(RayNodeSchema)),
	error: v.optional(v.nullable(v.string())),
});
export type RayClusterPayload = v.InferOutput<typeof RayClusterPayloadSchema>;

export async function rayHealth(fetchFn: typeof fetch = fetch): Promise<RayHealth> {
	const res = await fetchFn('/api/ray/health');
	if (!res.ok) throw new Error(`rayHealth: HTTP ${res.status}`);
	return parse(RayHealthSchema, await res.json());
}

export async function rayJobs(fetchFn: typeof fetch = fetch): Promise<RayJobsPayload> {
	const res = await fetchFn('/api/ray/jobs');
	if (!res.ok) throw new Error(`rayJobs: HTTP ${res.status}`);
	return parse(RayJobsPayloadSchema, await res.json());
}

export async function rayCluster(fetchFn: typeof fetch = fetch): Promise<RayClusterPayload> {
	const res = await fetchFn('/api/ray/cluster');
	if (!res.ok) throw new Error(`rayCluster: HTTP ${res.status}`);
	return parse(RayClusterPayloadSchema, await res.json());
}

export const ActorInfoSchema = v.object({
	actor_id: v.nullable(v.string()),
	class_name: v.string(),
	name: v.nullable(v.string()),
	repr_name: v.nullable(v.string()),
	state: v.string(),
	pid: v.nullable(v.number()),
	node_id: v.nullable(v.string()),
	job_id: v.nullable(v.string()),
	ray_namespace: v.nullable(v.string()),
	num_restarts: v.number(),
	is_detached: v.boolean(),
	placement_group_id: v.nullable(v.string()),
	required_resources: v.record(v.string(), v.number()),
	death_reason: v.nullable(v.string()),
	// Live telemetry (from /logical/actors; null when unavailable).
	start_time_ms: v.nullable(v.number()),
	end_time_ms: v.nullable(v.number()),
	cpu_percent: v.nullable(v.number()),
	rss_bytes: v.nullable(v.number()),
	num_fds: v.nullable(v.number()),
	gpu_util: v.nullable(v.number()),
	gpu_mem_mb: v.nullable(v.number()),
	num_executed_tasks: v.nullable(v.number()),
	num_running_tasks: v.nullable(v.number()),
	num_pending_tasks: v.nullable(v.number()),
	task_queue_length: v.nullable(v.number()),
	ip_address: v.nullable(v.string()),
	worker_id: v.nullable(v.string()),
});
export type ActorInfo = v.InferOutput<typeof ActorInfoSchema>;

export const ActorsPayloadSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	actors: v.array(ActorInfoSchema),
	error: v.optional(v.nullable(v.string())),
});
export type ActorsPayload = v.InferOutput<typeof ActorsPayloadSchema>;

/** Ray actors, merged from the state API + /logical/actors by the ray-api service. */
export async function actorsList(fetchFn: typeof fetch = fetch): Promise<ActorInfo[]> {
	const res = await fetchFn('/api/ray/actors');
	if (!res.ok) throw new Error(`actorsList: HTTP ${res.status}`);
	const payload = parse(ActorsPayloadSchema, await res.json());
	if (!payload.ok) throw new Error(payload.error ?? 'actors unavailable');
	return payload.actors ?? [];
}

export const TaskInfoSchema = v.object({
	task_id: v.nullable(v.string()),
	name: v.nullable(v.string()),
	func_or_class_name: v.nullable(v.string()),
	type: v.nullable(v.string()),
	state: v.string(),
	job_id: v.nullable(v.string()),
	actor_id: v.nullable(v.string()),
	node_id: v.nullable(v.string()),
	worker_pid: v.nullable(v.number()),
	attempt_number: v.nullable(v.number()),
	error_type: v.nullable(v.string()),
	error_message: v.nullable(v.string()),
	required_resources: v.record(v.string(), v.number()),
	creation_time_ms: v.nullable(v.number()),
	start_time_ms: v.nullable(v.number()),
	end_time_ms: v.nullable(v.number()),
});
export type TaskInfo = v.InferOutput<typeof TaskInfoSchema>;

export const TasksPayloadSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	tasks: v.array(TaskInfoSchema),
	error: v.optional(v.nullable(v.string())),
});
export type TasksPayload = v.InferOutput<typeof TasksPayloadSchema>;

export async function tasksList(fetchFn: typeof fetch = fetch): Promise<TaskInfo[]> {
	const res = await fetchFn('/api/ray/tasks');
	if (!res.ok) throw new Error(`tasksList: HTTP ${res.status}`);
	const payload = parse(TasksPayloadSchema, await res.json());
	if (!payload.ok) throw new Error(payload.error ?? 'tasks unavailable');
	return payload.tasks ?? [];
}

export const RayEventSchema = v.object({
	event_id: v.nullable(v.string()),
	severity: v.string(),
	message: v.string(),
	time: v.nullable(v.string()),
	source_type: v.nullable(v.string()),
});
export type RayEvent = v.InferOutput<typeof RayEventSchema>;

export const OverviewPayloadSchema = v.object({
	ok: v.boolean(),
	dashboard_url: v.string(),
	ray_version: v.nullable(v.string()),
	session_name: v.nullable(v.string()),
	events: v.array(RayEventSchema),
	error: v.optional(v.nullable(v.string())),
});
export type OverviewPayload = v.InferOutput<typeof OverviewPayloadSchema>;

export async function rayOverview(fetchFn: typeof fetch = fetch): Promise<OverviewPayload> {
	const res = await fetchFn('/api/ray/overview');
	if (!res.ok) throw new Error(`rayOverview: HTTP ${res.status}`);
	return parse(OverviewPayloadSchema, await res.json());
}

export const JobLogsPayloadSchema = v.object({
	ok: v.boolean(),
	submission_id: v.string(),
	logs: v.string(),
	error: v.optional(v.nullable(v.string())),
});
export type JobLogsPayload = v.InferOutput<typeof JobLogsPayloadSchema>;

export async function rayJobLogs(
	submissionId: string,
	tail = 2000,
	fetchFn: typeof fetch = fetch,
): Promise<JobLogsPayload> {
	const res = await fetchFn(`/api/ray/jobs/${encodeURIComponent(submissionId)}/logs?tail=${tail}`);
	if (!res.ok) throw new Error(`rayJobLogs: HTTP ${res.status}`);
	return parse(JobLogsPayloadSchema, await res.json());
}

export const LogsPayloadSchema = v.object({
	ok: v.boolean(),
	node_id: v.nullable(v.string()),
	filename: v.nullable(v.string()),
	files: v.record(v.string(), v.array(v.string())),
	text: v.nullable(v.string()),
	error: v.optional(v.nullable(v.string())),
});
export type LogsPayload = v.InferOutput<typeof LogsPayloadSchema>;

export async function rayLogFiles(
	nodeId: string,
	fetchFn: typeof fetch = fetch,
): Promise<LogsPayload> {
	const res = await fetchFn(`/api/ray/logs?node_id=${encodeURIComponent(nodeId)}`);
	if (!res.ok) throw new Error(`rayLogFiles: HTTP ${res.status}`);
	return parse(LogsPayloadSchema, await res.json());
}

export async function rayLogContent(
	nodeId: string,
	filename: string,
	lines = 500,
	fetchFn: typeof fetch = fetch,
): Promise<LogsPayload> {
	const qs = new URLSearchParams({ node_id: nodeId, filename, lines: String(lines) });
	const res = await fetchFn(`/api/ray/logs?${qs}`);
	if (!res.ok) throw new Error(`rayLogContent: HTTP ${res.status}`);
	return parse(LogsPayloadSchema, await res.json());
}

export const ServeReplicaSchema = v.object({
	replica_id: v.string(),
	state: v.string(),
	node_id: v.optional(v.string()),
	node_ip: v.optional(v.string()),
	node_instance_id: v.optional(v.string()),
	actor_name: v.optional(v.string()),
	pid: v.optional(v.number()),
	start_time_s: v.optional(v.number()),
	log_file_path: v.optional(v.string()),
});
export type ServeReplica = v.InferOutput<typeof ServeReplicaSchema>;

export const ServeRuntimeEnvSchema = v.object({
	uv: v.optional(v.array(v.string())),
	pip: v.optional(v.array(v.string())),
	working_dir: v.optional(v.string()),
	env_vars: v.optional(v.record(v.string(), v.string())),
});
export type ServeRuntimeEnv = v.InferOutput<typeof ServeRuntimeEnvSchema>;

export const ServeRayActorOptionsSchema = v.object({
	num_cpus: v.optional(v.number()),
	num_gpus: v.optional(v.number()),
	resources: v.optional(v.record(v.string(), v.number())),
	runtime_env: v.optional(ServeRuntimeEnvSchema),
});
export type ServeRayActorOptions = v.InferOutput<typeof ServeRayActorOptionsSchema>;

export const ServeDeploymentConfigSchema = v.object({
	num_replicas: v.optional(v.number()),
	max_ongoing_requests: v.optional(v.number()),
	max_queued_requests: v.optional(v.number()),
	health_check_period_s: v.optional(v.number()),
	health_check_timeout_s: v.optional(v.number()),
	graceful_shutdown_wait_loop_s: v.optional(v.number()),
	graceful_shutdown_timeout_s: v.optional(v.number()),
	rolling_update_percentage: v.optional(v.number()),
	autoscaling_config: v.optional(v.nullable(v.record(v.string(), v.unknown()))),
	user_config: v.optional(v.unknown()),
	ray_actor_options: v.optional(ServeRayActorOptionsSchema),
	request_router_config: v.optional(v.object({ request_router_class: v.optional(v.string()) })),
});
export type ServeDeploymentConfig = v.InferOutput<typeof ServeDeploymentConfigSchema>;

export const ServeDeploymentSchema = v.object({
	name: v.string(),
	status: v.string(),
	status_trigger: v.optional(v.string()),
	message: v.optional(v.string()),
	target_num_replicas: v.optional(v.number()),
	required_resources: v.optional(v.record(v.string(), v.number())),
	replicas: v.array(ServeReplicaSchema),
	recent_dead_replicas: v.optional(v.array(ServeReplicaSchema)),
	deployment_config: v.optional(ServeDeploymentConfigSchema),
});
export type ServeDeployment = v.InferOutput<typeof ServeDeploymentSchema>;

export const ServeTopologyNodeSchema = v.object({
	name: v.string(),
	is_ingress: v.boolean(),
	outbound_deployments: v.array(v.object({ name: v.string() })),
});
export type ServeTopologyNode = v.InferOutput<typeof ServeTopologyNodeSchema>;

export const ServeTopologySchema = v.object({
	app_name: v.string(),
	nodes: v.record(v.string(), ServeTopologyNodeSchema),
	ingress_deployment: v.optional(v.string()),
});
export type ServeTopology = v.InferOutput<typeof ServeTopologySchema>;

export const ServeApplicationSchema = v.object({
	name: v.string(),
	route_prefix: v.nullable(v.string()),
	docs_path: v.nullable(v.string()),
	status: v.string(),
	message: v.optional(v.string()),
	source: v.optional(v.string()),
	external_scaler_enabled: v.optional(v.boolean()),
	last_deployed_time_s: v.optional(v.number()),
	deployments: v.record(v.string(), ServeDeploymentSchema),
	deployment_topology: v.optional(ServeTopologySchema),
});
export type ServeApplication = v.InferOutput<typeof ServeApplicationSchema>;

export const ServeProxySchema = v.object({
	status: v.optional(v.string()),
	node_id: v.optional(v.string()),
	node_ip: v.optional(v.string()),
	node_instance_id: v.optional(v.string()),
	log_file_path: v.optional(v.string()),
});
export type ServeProxy = v.InferOutput<typeof ServeProxySchema>;

export const ServeControllerInfoSchema = v.object({
	node_ip: v.optional(v.string()),
	node_instance_id: v.optional(v.string()),
	log_file_path: v.optional(v.string()),
});
export type ServeControllerInfo = v.InferOutput<typeof ServeControllerInfoSchema>;

export const ServeControllerHealthSchema = v.object({
	uptime_s: v.optional(v.number()),
	num_control_loops: v.optional(v.number()),
	loops_per_second: v.optional(v.number()),
	num_asyncio_tasks: v.optional(v.number()),
});
export type ServeControllerHealth = v.InferOutput<typeof ServeControllerHealthSchema>;

export const ServeTargetGroupSchema = v.object({
	targets: v.array(
		v.object({
			ip: v.string(),
			port: v.number(),
			instance_id: v.optional(v.string()),
			name: v.optional(v.string()),
		}),
	),
});
export type ServeTargetGroup = v.InferOutput<typeof ServeTargetGroupSchema>;

/** Raw Ray Serve status, proxied straight from the dashboard's /api/serve/applications/. */
export const ServePayloadSchema = v.object({
	applications: v.record(v.string(), ServeApplicationSchema),
	proxies: v.optional(v.record(v.string(), ServeProxySchema)),
	proxy_location: v.optional(v.string()),
	http_options: v.optional(
		v.object({ host: v.optional(v.string()), port: v.optional(v.number()) }),
	),
	grpc_options: v.optional(v.object({ port: v.optional(v.number()) })),
	target_capacity: v.optional(v.nullable(v.number())),
	controller_info: v.optional(ServeControllerInfoSchema),
	controller_health_metrics: v.optional(ServeControllerHealthSchema),
	target_groups: v.optional(v.array(ServeTargetGroupSchema)),
});
export type ServePayload = v.InferOutput<typeof ServePayloadSchema>;

export async function serveApplications(fetchFn: typeof fetch = fetch): Promise<ServePayload> {
	const res = await fetchFn('/api/serve/applications/');
	if (!res.ok) throw new Error(`serveApplications: HTTP ${res.status}`);
	return parse(ServePayloadSchema, await res.json());
}
