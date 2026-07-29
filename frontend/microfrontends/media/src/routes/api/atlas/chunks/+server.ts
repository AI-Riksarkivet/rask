import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Explicit POST route: the atlas selection's batched row fetch (`{ rowids }` → rows) —
// a read spelled as a POST (the rowid list outgrows a query string). Bearer-forwarding,
// fail-closed without a session on an auth-enabled stack; GETs ride the /api catch-all.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

export const POST = makeBackendProxy({
	backendUrl: VIEWER_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
