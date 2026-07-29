import { createContext } from 'svelte';
import type { ActorInfo, RayClusterPayload, RayJobsPayload } from '@rask/api';

/**
 * The Ray reads the compute panels need — INJECTED by the zone, not imported.
 *
 * The panels used to `import { getRayJobs } from '$lib/remote/compute.remote'`, which cannot survive a
 * move into a package for a STRUCTURAL reason rather than an alias one: SvelteKit hashes a remote
 * function's endpoint id from its path RELATIVE TO THE APP and serves it at
 * `${base}/${app_dir}/remote/${id}`. A package-shipped `.remote.ts` would be re-registered and
 * re-served per zone, and would execute under whichever zone's `handleFetch` loaded it — which for
 * `lakehouse` defaults to `LANCE_GATEWAY_URL` (:8001, the lineage service) rather than the gateway.
 * Remote functions are per-app by construction; the panels take the RESULTS instead.
 *
 * The payload types come from `@rask/api`, which is where the queries get them — hand-rolling shapes
 * here would drift the moment the Ray API changed, and drift silently, because a panel renders
 * `undefined` as an empty list rather than an error.
 *
 * Each field is whatever a zone's `query()` returns: an object carrying `.current`. Declared
 * structurally so this package never imports `$app/server` — the same stance `@rask/ui` takes.
 */
export interface ComputeReads {
	getRayJobs(): { readonly current: RayJobsPayload | undefined };
	getRayCluster(): { readonly current: RayClusterPayload | undefined };
	getActors(): { readonly current: ActorInfo[] | undefined };
}

export const [getComputeReads, setComputeReads] = createContext<ComputeReads>();
