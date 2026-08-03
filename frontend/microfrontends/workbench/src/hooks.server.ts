import { env } from '$env/dynamic/private';
import { makeZoneHooks } from '@rask/api/bff';
import { makeZoneServerErrorHandler } from '@rask/api/observability';

// Per-request session hydration from the sealed OIDC cookie + the SSR `/api/*` → in-cluster gateway
// rewrite, both single-sourced in @rask/api/bff — the SAME wiring as every other BFF zone. This was
// the review's critical finding: the first cut copied compute's handleFetch-only shape, so
// locals.session was never set and the capi user-state proxy forwarded every layout/view write to
// the OIDC-only catalog WITHOUT a bearer — per-user persistence silently 401'd for signed-in users.
export const { handle, handleFetch } = makeZoneHooks(env, { gateway: true });

export const handleError = makeZoneServerErrorHandler('workbench');
