import { env } from '$env/dynamic/private';
import { makeCatalogProxy } from '@rask/api/bff';

/**
 * The caller's own user-state documents on the catalog — for this zone, the dock workbench layouts.
 *
 * This zone already mounts `capi/[...path]`, but that catch-all is **GET-only** on purpose, and widening
 * it would be the wrong fix: `makeCatalogProxy` forwards the signed-in bearer to the whole catalog, so a
 * `PUT` on the catch-all would expose warehouse mutation, grant writes and table drops through this
 * zone's origin. A dock layout needs exactly three verbs on exactly one path, so it gets its own NAMED
 * route — the same call the media zone made for the same reason, and SvelteKit resolves the specific
 * route ahead of the catch-all.
 *
 * `[document]` is a closed enum on the server side (`UserStateDocument`), never an identity. The catalog
 * derives the subject from the verified bearer this proxy forwards; there is no user parameter anywhere
 * in the contract, which is what makes a cross-user read impossible rather than merely unlikely.
 */
export const GET = makeCatalogProxy(env);
export const PUT = makeCatalogProxy(env);
export const DELETE = makeCatalogProxy(env);
