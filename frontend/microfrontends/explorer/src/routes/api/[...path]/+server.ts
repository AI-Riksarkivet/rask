import { env } from '$env/dynamic/private';
import { makeViewerProxy } from '@rask/api/bff';

// Same-origin, GET-only read proxy to the VIEWER service — this zone's media-plane catch-all (Range and
// the upstream response headers ride through). The search + annotations/assist/jobs domains have their
// own more-specific routes beside this one. Single-sourced in @rask/api/bff.
export const GET = makeViewerProxy(env);
