import { env } from '$env/dynamic/private';
import { makeCatalogProxy } from '@rask/api/bff';

/**
 * The caller's own user-state documents on the catalog — for this zone, its dock workbench layout.
 *
 * This is the zone's FIRST `capi` route. Compute otherwise reaches everything through the gateway
 * (`makeGatewayHandleFetch`), which is a dumb proxy that attaches no credential — fine for the Ray
 * dashboard surfaces it reads, useless for a per-subject document the catalog authorizes. So the
 * layout takes the same door media and the lakehouse use: a NAMED route forwarding only the signed-in
 * user's bearer.
 *
 * Named `[document]` rather than a `capi/[...path]` catch-all, deliberately and for the same reason
 * the other zones did it: a catch-all would expose the catalog's whole mutation surface — warehouses,
 * grants, table drops — through this zone's origin, to buy three verbs on one path.
 *
 * Identity is never in the path. The catalog derives the subject from the verified bearer this proxy
 * forwards; there is no user parameter anywhere in the contract.
 */
export const GET = makeCatalogProxy(env);
export const PUT = makeCatalogProxy(env);
export const DELETE = makeCatalogProxy(env);
