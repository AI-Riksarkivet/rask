import { env } from '$env/dynamic/private';
import { makeCatalogProxy } from '@rask/api/bff';

/**
 * The caller's own workflow canvas and saved views, on the catalog's per-subject user-state endpoints.
 *
 * This zone owns both documents, and until now it could not reach them: media had exactly ONE catalog
 * route (`capi/v1/me`), so `GET /media/capi/v1/user-state/workflow-graph` was a 404 — a SvelteKit HTML
 * page — while the identical path answered 200 in the lakehouse zone, which happens to mount a
 * `capi/[...path]` catch-all. A server nobody can call is not a feature.
 *
 * A NAMED `[document]` parameter rather than that catch-all, deliberately. A catch-all here would expose
 * the catalog's entire surface — warehouses, grants, table mutation — through the media zone's origin,
 * and this zone needs two documents. The catalog still authorises every call on its own; this is defence
 * in depth, not the only lock, and it keeps the zone's reachable surface readable from its route tree.
 *
 * Identity is never in the path. `[document]` is `workflow-graph` or `saved-views` — the catalog derives
 * the subject from the verified bearer this proxy forwards, and there is no user parameter anywhere in
 * the contract. A route that accepted one would be a cross-user read one missing check away.
 *
 * PUT and DELETE as well as GET: this is where a canvas is saved, and a read-only door would leave the
 * work in `localStorage` — which is the whole thing being fixed.
 */
export const GET = makeCatalogProxy(env);
export const PUT = makeCatalogProxy(env);
export const DELETE = makeCatalogProxy(env);
