import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// "What can this subject reach?" — POST /v1/access/list-objects. Estate-admin gated BY THE CATALOG;
// this route only bearer-forwards the session, never a service token.
//
// `requireSession` for the same reason /access/check carries it: enumerating one subject's whole reach
// IS an ACL disclosure, so an anonymous request must be refused HERE rather than left to leave the BFF
// and rely on the catalog to say no.
//
// Unlike /access/tuples this does not enumerate the body — that route rebuilds {user, relation, object}
// because it is a WRITE and the proxy must be incapable of issuing any other shape. This is a read whose
// every field the catalog validates against the compiled model (unknown type or phantom relation → 400),
// so re-listing the fields here would only add a second contract to drift.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const POST = makeBackendProxy({
	backendUrl: CATALOG_API,
	stripPrefix: /^\/capi/,
	requireSession: true,
});
