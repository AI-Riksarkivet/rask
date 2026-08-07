import * as v from 'valibot';
import { query, getRequestEvent } from '$app/server';
import {
	rayOverview,
	rayJobs,
	rayCluster,
	rayHealth,
	rayLogFiles,
	rayLogContent,
	actorsList,
	tasksList,
	serveApplications,
	rayJobLogs,
	type OverviewPayload,
	type RayJobsPayload,
	type RayClusterPayload,
	type RayHealth,
	type LogsPayload,
	type ActorInfo,
	type TaskInfo,
	type ServePayload,
	type JobLogsPayload,
} from '@rask/api';

// Compute (Ray/cluster) microfrontend data layer — SvelteKit remote functions
// (server-only). The batches/chunks reads died at P7a with the batches table — the
// landing's pipeline surface is the lineage runs feed (lib/live/feeds.remote.ts).
//
// THE ONE PATTERN: every read is a `query()` whose body calls a `@rask/api`
// function, passing `getRequestEvent().fetch`. That `event.fetch` is SvelteKit's
// request-scoped fetch (same one `load` receives): it resolves the relative
// `/api/*` URLs `@rask/api` uses against the request origin during SSR (a bare
// global `fetch` has no origin on the server), inherits cookies, and inlines the
// response into the SSR payload so hydration doesn't refetch. So the query()
// SSR-renders the first frame (no onMount waterfall) and REUSES @rask/api's
// schemas + parse — zero duplicated fetch/validation per app.
//
// No-arg queries (cache key is the function identity); the one param query
// (`getRayJobLogs`) takes a valibot schema so navigating the submission id
// re-keys it. All read-only — writes stay direct @rask/api calls in handlers.

/** Cluster overview (events feed + ray/session info) — polled every 5s. The
 *  dashboard proxy never 5xxs (offline-safe payload). */
export const getOverview = query(async (): Promise<OverviewPayload> => {
	return rayOverview(getRequestEvent().fetch);
});

/** Ray jobs — polled every 5s via `.refresh()`. The dashboard proxy never 5xxs
 *  (returns `{ ok: false, error }`), so this resolves even when Ray is down. */
export const getRayJobs = query(async (): Promise<RayJobsPayload> => {
	return rayJobs(getRequestEvent().fetch);
});

/** Ray cluster resources/nodes — polled every 5s. Same offline-safe contract. */
export const getRayCluster = query(async (): Promise<RayClusterPayload> => {
	return rayCluster(getRequestEvent().fetch);
});

/** Live actors — polled every 5s. Returns `[]` (never 5xx) when Ray is down. */
export const getActors = query(async (): Promise<ActorInfo[]> => {
	return actorsList(getRequestEvent().fetch);
});

/** Cluster tasks — polled every 5s (job detail uses these). */
export const getTasks = query(async (): Promise<TaskInfo[]> => {
	return tasksList(getRequestEvent().fetch);
});

/** ONE job's tasks, filtered server-side (#140) — the detail page's transport. */
export const getJobTasks = query(
	v.object({ jobId: v.string() }),
	async ({ jobId }): Promise<TaskInfo[]> => {
		return tasksList(getRequestEvent().fetch, jobId);
	},
);

/** Serve applications/deployments — polled every 5s. Offline-safe payload. */
export const getServe = query(async (): Promise<ServePayload> => {
	return serveApplications(getRequestEvent().fetch);
});

/** Driver logs for one submission id. Param query so navigating the id re-keys
 *  it; polled while the job is RUNNING/PENDING. Offline-safe (`{ ok:false }`). */
export const getRayJobLogs = query(
	v.object({ id: v.string(), tail: v.optional(v.number()) }),
	async ({ id, tail }): Promise<JobLogsPayload> => {
		return rayJobLogs(id, tail ?? 2000, getRequestEvent().fetch);
	},
);

/** Ray cluster health — the live "is it up?" signal at the top of the overview.
 *  Polled every 5s (offline-safe payload). */
export const getRayHealth = query(async (): Promise<RayHealth> => {
	return rayHealth(getRequestEvent().fetch);
});

/** Log-file inventory for one node (grouped by category). Param query keyed by
 *  `{ nodeId }` so switching node re-keys it. Offline-safe (`{ ok:false }`). */
export const getLogFiles = query(
	v.object({ nodeId: v.string() }),
	async ({ nodeId }): Promise<LogsPayload> => {
		return rayLogFiles(nodeId, getRequestEvent().fetch);
	},
);

/** Tail content of one log file on one node. Param query keyed by
 *  `{ nodeId, filename, lines }` so any of them changing re-keys it; polled
 *  while following. Offline-safe (`{ ok:false }`). */
export const getLogContent = query(
	v.object({ nodeId: v.string(), filename: v.string(), lines: v.optional(v.number()) }),
	async ({ nodeId, filename, lines }): Promise<LogsPayload> => {
		return rayLogContent(nodeId, filename, lines ?? 500, getRequestEvent().fetch);
	},
);
