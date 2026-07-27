import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Same-origin proxy to the ANNOTATOR service for one unit's annotations. GET returns
// Arrow IPC + the X-Annotations-Version header (the optimistic-concurrency handshake),
// so the upstream's response headers pass through. POST is the enumerated SAVE endpoint
// (the batched delta + 409 contract on this same path) — bearer-forwarding and
// fail-closed without a signed-in user on an auth-enabled stack (the confused-deputy
// stance: a write must be attributable).
const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

export const GET = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: KEEP_API_PREFIX,
	forwardResponseHeaders: true,
});

export const POST = makeBackendProxy({
	backendUrl: ANNOTATOR_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
