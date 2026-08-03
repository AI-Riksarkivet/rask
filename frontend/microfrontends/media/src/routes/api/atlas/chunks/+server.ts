import { env } from '$env/dynamic/private';
import { makeArrowRowsProxy } from '$lib/server/rows-arrow';

// Explicit POST route: the atlas selection's batched row fetch (`{ rowids }` → rows) — a read spelled
// as a POST (the rowid list outgrows a query string). Bearer-forwarding, fail-closed without a session
// on an auth-enabled stack; GETs ride the /api catch-all.
//
// The RESPONSE is Arrow IPC (open_transport.md's first promotion): a lasso hands back up to 1000 full
// corpus rows, which was the estate's clearest `list[dict]` offender. The request shape is unchanged and
// so is every credential; only the success body is columnar now, decoded in `@rask/media-api` with the
// same `tableFromIPC` the atlas projection beside it already uses.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

export const POST = makeArrowRowsProxy({
	backendUrl: VIEWER_API,
	path: '/api/atlas/chunks',
	requireSession: true,
});
