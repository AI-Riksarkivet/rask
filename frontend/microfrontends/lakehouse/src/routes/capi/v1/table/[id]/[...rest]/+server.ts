import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// GET subpaths under a table id (detail, versions, …): the static capi/v1/table/[id]/ branch
// shadows the capi/[...path] catch-all for its whole subtree and SvelteKit does not backtrack
// out of it — so without this rest route every GET child not explicitly enumerated 404s (third
// instance of the class: the bare list, then the per-id detail — found live 2026-07-24 as the
// table page's phantom "no read access" state). GET-only on purpose: writes stay explicit
// per-action routes (the confused-deputy stance).
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const GET = makeBackendProxy({ backendUrl: CATALOG_API, stripPrefix: /^\/capi/ });
