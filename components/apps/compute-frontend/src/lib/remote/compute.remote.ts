import * as v from 'valibot';
import { query, getRequestEvent } from '$app/server';
import {
	rayOverview,
	rayJobs,
	rayCluster,
	actorsList,
	tasksList,
	serveApplications,
	rayJobLogs,
	listBatches,
	type OverviewPayload,
	type RayJobsPayload,
	type RayClusterPayload,
	type ActorInfo,
	type TaskInfo,
	type ServePayload,
	type JobLogsPayload,
	type BatchesPayload,
} from '@rask/api';

// Compute (Ray/cluster) microfrontend data layer — SvelteKit remote functions
// (server-only). Identical pattern to overview-frontend's overview.remote.ts.
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

/** Serve applications/deployments — polled every 5s. Offline-safe payload. */
export const getServe = query(async (): Promise<ServePayload> => {
	return serveApplications(getRequestEvent().fetch);
});

/** Batch inventory — used by the job-detail progress card (slower cadence). */
export const getBatches = query(async (): Promise<BatchesPayload> => {
	return listBatches(getRequestEvent().fetch);
});

/** Driver logs for one submission id. Param query so navigating the id re-keys
 *  it; polled while the job is RUNNING/PENDING. Offline-safe (`{ ok:false }`). */
export const getRayJobLogs = query(
	v.object({ id: v.string(), tail: v.optional(v.number()) }),
	async ({ id, tail }): Promise<JobLogsPayload> => {
		return rayJobLogs(id, tail ?? 2000, getRequestEvent().fetch);
	},
);
