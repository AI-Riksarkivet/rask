import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// The storage browser's backend seam (R18): `/api/v1/volumes/**` rides to the rask GATEWAY with its
// path unchanged (KEEP_API_PREFIX strips nothing), and the gateway path-routes it to volumes-api.
// More specific than the `/api/[...path]` lineage catch-all beside it, so SvelteKit routes here
// first. GET-only; response headers are forwarded so the download route's content-type +
// content-disposition reach the browser intact.
export const GET = makeBackendProxy({
	backendUrl: env.RASK_GATEWAY_URL ?? 'http://localhost:8888',
	stripPrefix: KEEP_API_PREFIX,
	forwardResponseHeaders: true,
});
