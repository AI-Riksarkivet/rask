import type { HandleFetch } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { edgeUrl, makeEdgeHandleFetch } from '@rask/api';
import { makeOidcConfig, makeSessionHandle } from '@rask/api/bff';
import { makeZoneServerErrorHandler } from '@rask/api/observability';

// Session hydration from the sealed OIDC cookie — the flows BFF (`api/infer`)
// refuses bearer-less writes on a governed stack, and without this handle the
// session is never populated, which fails SILENTLY on an auth-off dev stack
// (the models zone hit exactly this when it grew its first credentialed
// surface). Same wiring as compute.
export const handle = makeSessionHandle(makeOidcConfig(env));

// SSR reads (remote query() via getRequestEvent().fetch) issue relative
// /api/*, which in prod resolves against the external ingress origin and
// hairpins back through the ingress. Route them to the cluster edge's
// in-cluster address (RASK_EDGE_URL) instead; client-side fetches are untouched.
export const handleFetch: HandleFetch = makeEdgeHandleFetch(edgeUrl(env));

// Every unhandled SSR/endpoint error is stamped with THIS zone, so an alert
// names the zone that can fix it — the zones share one origin.
export const handleError = makeZoneServerErrorHandler('studio');
