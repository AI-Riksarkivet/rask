import { query, getRequestEvent } from '$app/server';
import {
	listBatches,
	listChunks,
	rayJobs,
	rayCluster,
	type BatchesPayload,
	type ChunkRow,
	type RayJobsPayload,
	type RayClusterPayload,
} from '@rask/api';

// Overview microfrontend data layer — SvelteKit remote functions (server-only).
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
// No-arg queries (cache key is the function identity); param queries take a
// valibot schema (see discover's viewer). All read-only — writes stay commands
// (uploadVolume/syncBatches/submitChunk are mutations, called directly).

/** Batch inventory + summary tiles. SSR-rendered initial frame. */
export const getBatches = query(async (): Promise<BatchesPayload> => {
	return listBatches(getRequestEvent().fetch);
});

/** Chunk strip rows. SSR-rendered initial frame. */
export const getChunks = query(async (): Promise<ChunkRow[]> => {
	const { chunks } = await listChunks(getRequestEvent().fetch);
	return chunks;
});

/** Ray jobs — polled every 5s via `.refresh()`. The dashboard proxy never 5xxs
 *  (returns `{ ok: false, error }`), so this resolves even when Ray is down. */
export const getRayJobs = query(async (): Promise<RayJobsPayload> => {
	return rayJobs(getRequestEvent().fetch);
});

/** Ray cluster resources — polled every 5s via `.refresh()`. Same offline-safe
 *  contract as `getRayJobs`. */
export const getRayCluster = query(async (): Promise<RayClusterPayload> => {
	return rayCluster(getRequestEvent().fetch);
});
