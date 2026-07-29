import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// The storage browser's backend seam (R18): `/api/media/**` rides to the rask GATEWAY with its
// path unchanged (KEEP_API_PREFIX strips nothing), and the gateway's `/api/media` row routes it to
// the media-plane viewer, which serves the ported S3 object browser (`/api/media/objects` →
// viewer `/api/objects`; volumes-api retired in the R6/R20 wave). More specific than the
// `/api/[...path]` lineage catch-all beside it, so SvelteKit routes here first. GET-only;
// response headers are forwarded so the download route's content-type + content-disposition
// reach the browser intact.
export const GET = makeBackendProxy({
	backendUrl: env.RASK_GATEWAY_URL ?? 'http://localhost:8888',
	stripPrefix: KEEP_API_PREFIX,
	forwardResponseHeaders: true,
});
