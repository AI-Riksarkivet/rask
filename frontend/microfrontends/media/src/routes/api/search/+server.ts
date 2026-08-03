import { env } from '$env/dynamic/private';
import { makeArrowRowsProxy } from '$lib/server/rows-arrow';

// Same-origin proxy to the SEARCH service. GET is the text search; POST is the SAME query surface with
// an image attached (multipart query-by-example) — a read spelled as a POST, so it gets its own explicit
// route (no blanket write proxy) and, like every non-GET here, fails closed without a signed-in user on
// an auth-enabled stack.
//
// That multipart POST is also why this stays a `+server.ts` at all: a remote function cannot carry a
// File. What DID move is the response — the same `Row[]` payload kind as the atlas selection, now Arrow
// IPC (open_transport.md's second promotion, taken for transport uniformity rather than for n≤100's
// bytes). Errors stay JSON: the client reads `detail` out of them.
const SEARCH_API = env.SEARCH_API ?? 'http://localhost:8102';

export const GET = makeArrowRowsProxy({ backendUrl: SEARCH_API, path: '/api/search' });

export const POST = makeArrowRowsProxy({
	backendUrl: SEARCH_API,
	path: '/api/search',
	requireSession: true,
});
