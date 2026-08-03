import { env } from '$env/dynamic/private';
import { makeAtlasPointsHandler } from '$lib/server/atlas-points';

// Explicit GET route ahead of the `/api/[...path]` viewer catch-all: the 6.6 MB atlas projection, served
// from this zone's server-side cache. Authorized on every request (session, fail-closed, plus a probe of
// the same resource with the caller's own bearer), keyed on the resource and its version rather than the
// caller, and single-flighted so ten cold hits are one upstream read. See $lib/server/atlas-points.ts for
// the measurement, the authorization argument, and what an in-process store costs at replicas: 2.
export const GET = makeAtlasPointsHandler(env);
