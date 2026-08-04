import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Explicit POST route: voice query-by-example with an UPLOADED sample (multipart) — the
// bytes-preserving proxy forwards the body untouched. Bearer-forwarding, fail-closed
// without a session on an auth-enabled stack; the anchor-based GET rides the catch-all.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

export const POST = makeBackendProxy({
	backendUrl: VIEWER_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
