/**
 * Where each upstream answers, as the browser reaches it: same-origin paths the platform's Gateway
 * routes to the owning service. Nothing here is per environment, so the built app is the same bytes on
 * every estate.
 */

/** The catalog, published unrewritten under `/api/catalog` (it strips the prefix itself). */
export const CATALOG_API = '/api/catalog';
/** Lineage, published under `/api/lineage` and rewritten to its root. */
export const LINEAGE_API = '/api/lineage';
/** The notifications inbox, which mounts its own routes under `/api/notifications`. */
export const NOTIFICATIONS_API = '/api/notifications';
