import { env } from '$env/dynamic/private';
import { makeLineageProxy } from '@rask/api/bff';

// Same-origin, GET-only pass-through to the LINEAGE service (the /capi proxy covers catalog), with the
// READ-only service-credential fallback. Single-sourced in @rask/api/bff.
export const GET = makeLineageProxy(env);
