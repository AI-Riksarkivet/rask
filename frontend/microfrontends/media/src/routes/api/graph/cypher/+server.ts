import { env } from '$env/dynamic/private';
import { KEEP_API_PREFIX, makeBackendProxy } from '@rask/api/bff';

// Explicit POST route: the knowledge-graph Cypher console (`{ query, limit }`). A
// user-authored query is the closest thing the read plane has to a write surface, so it
// is enumerated here (no blanket write proxy) and fails closed without a session on an
// auth-enabled stack; graph GETs (status/search/entity/subgraph) ride the /api catch-all.
const VIEWER_API = env.VIEWER_API ?? 'http://localhost:8101';

export const POST = makeBackendProxy({
	backendUrl: VIEWER_API,
	stripPrefix: KEEP_API_PREFIX,
	requireSession: true,
});
