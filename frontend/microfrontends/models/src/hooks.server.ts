import { env } from '$env/dynamic/private';
import { makeZoneHooks } from '@rask/api/bff';
import { makeZoneServerErrorHandler } from '@rask/api/observability';

// Per-request session hydration from the sealed OIDC cookie + the SSR `/api/*` → in-cluster gateway
// rewrite, both single-sourced in @rask/api/bff. No-op when OIDC is unconfigured (the zone runs
// auth-off).
//
// THIS ZONE DID NOT HAVE THIS FILE while it was `train`, and it did not need one: every page was a
// hardcoded placeholder array, so no request ever carried a credential. The registry that moved in
// from the lakehouse changes that — `remote/models.remote.ts` reads `locals.session?.accessToken`
// and the catalog is OIDC-only, so without this handle every read would go out bearer-less, 401, and
// the surface would render its "sign in" state to a user who is already signed in. That failure is
// silent on an auth-off dev stack and only appears on a governed one, which is exactly the class of
// bug the skill warns a moved auth-gated surface creates.
export const { handle, handleFetch } = makeZoneHooks(env, { gateway: true });

// Every unhandled SSR/endpoint error is stamped with THIS zone, so an alert names the zone that can
// fix it instead of "the page" — the zones share one origin, and nothing else can tell them apart.
export const handleError = makeZoneServerErrorHandler('models');
