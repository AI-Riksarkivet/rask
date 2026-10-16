import { env } from '$env/dynamic/private';
import { edgeUrl } from '@rask/api';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// The storage browser's backend seam (R18): `/api/explorer/**` rides to the cluster EDGE
// (`RASK_EDGE_URL`) with its path unchanged (KEEP_API_PREFIX strips nothing), and the edge's
// `/api/explorer` route rewrites it onto the media-plane viewer, which serves the S3 object browser
// (`/api/explorer/objects` → viewer `/api/objects`). More specific than the
// `/api/[...path]` lineage catch-all beside it, so SvelteKit routes here first. GET-only;
// response headers are forwarded so the download route's content-type + content-disposition
// reach the browser intact.
export const GET = makeBackendProxy({
	backendUrl: edgeUrl(env),
	stripPrefix: KEEP_API_PREFIX,
	forwardResponseHeaders: true,
});
