import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// "Would this grant do what I think?" — POST /v1/access/simulate. A Check evaluated as if the
// hypothetical tuples existed; the store is never written, because OpenFGA's contextual tuples are
// per-request.
//
// Estate-admin gated BY THE CATALOG, `requireSession` fail-closed here: a hypothetical Check discloses
// the graph exactly as a real one does, so it sits at the same bar rather than a lower one on the
// grounds that it changes nothing.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const POST = makeBackendProxy({
	backendUrl: CATALOG_API,
	stripPrefix: /^\/capi/,
	requireSession: true,
});
