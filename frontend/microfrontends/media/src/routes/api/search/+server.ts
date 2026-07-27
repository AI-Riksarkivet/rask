import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Same-origin proxy to the SEARCH service. GET is the text search; POST is the SAME
// query surface with an image attached (multipart query-by-example) — a read spelled
// as a POST, so it gets its own explicit route (no blanket write proxy) and, like every
// non-GET here, fails closed without a signed-in user on an auth-enabled stack.
const SEARCH_API = env.SEARCH_API ?? 'http://localhost:8102';

export const GET = makeBackendProxy({ backendUrl: SEARCH_API, stripPrefix: KEEP_API_PREFIX });

export const POST = makeBackendProxy({
	backendUrl: SEARCH_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
