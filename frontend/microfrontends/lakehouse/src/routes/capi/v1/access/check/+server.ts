import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// The FGA workbench's check probe: POST /v1/access/check → a live OpenFGA Check verdict on any
// (user, relation, object) triple. Estate-admin gated BY THE CATALOG (probing the graph == disclosing
// it); this route bearer-forwards the signed-in user's session and nothing else.
//
// `requireSession` is the same fail-closed stance every other write route here takes, and this was the
// one that did not: without it an anonymous request on an auth-enabled stack still LEFT the BFF and
// probed the authorization graph, relying entirely on the catalog to refuse it. It is refused here now.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const POST = makeBackendProxy({
	backendUrl: CATALOG_API,
	stripPrefix: /^\/capi/,
	requireSession: true,
});
