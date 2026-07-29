import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// "Who can do this?" — POST /v1/access/list-users. The inverse of list-objects, and the primitive the
// revoke confirm calls BEFORE it writes: a tuple high in the hierarchy can cut access for many
// principals at once, and the dialog has to be able to say how many rather than "are you sure?".
//
// Estate-admin gated BY THE CATALOG; session bearer-forwarded, `requireSession` fail-closed — the
// effective subject set of any object is exactly the disclosure this whole surface is gated on.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const POST = makeBackendProxy({
	backendUrl: CATALOG_API,
	stripPrefix: /^\/capi/,
	requireSession: true,
});
