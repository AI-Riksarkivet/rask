import { env } from '$env/dynamic/private';
import { makeZoneHooks } from '@rask/api/bff';
import { makeZoneServerErrorHandler } from '@rask/api/observability';

// Per-request session hydration from the sealed OIDC cookie + the SSR `/api/*` → cluster-edge
// rewrite, both single-sourced in @rask/api/bff. No-op when OIDC is unconfigured (the zone runs
// auth-off). The chart sets RASK_EDGE_URL in-cluster; dev defaults to `make dev-micro`'s dev edge.
export const { handle, handleFetch } = makeZoneHooks(env, { edge: true });

// Every unhandled SSR/endpoint error is stamped with THIS zone, so an alert names the zone that can fix
// it instead of "the page" — four zones share one origin, and nothing else can tell them apart.
export const handleError = makeZoneServerErrorHandler('lakehouse');
