import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// "Why does this resolve?" — POST /v1/access/expand. The derivation tree, optionally followed through
// the cascade (`depth`, clamped server-side).
//
// The recursion is deliberately the CATALOG's, not this route's. Walking the chain from the browser
// would cost one estate-gated, audited round trip per hop, under the shared client's fixed 8 s timeout —
// so a five-hop answer would be five audit rows and a likely abort. One request, one gate, one row.
//
// Estate-admin gated BY THE CATALOG; session bearer-forwarded, `requireSession` fail-closed.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const POST = makeBackendProxy({
	backendUrl: CATALOG_API,
	stripPrefix: /^\/capi/,
	requireSession: true,
});
