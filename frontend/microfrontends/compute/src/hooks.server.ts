import type { HandleFetch } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { edgeUrl, makeEdgeHandleFetch } from '@rask/api';
import { makeOidcConfig, makeSessionHandle } from '@rask/api/bff';

// Session hydration from the sealed OIDC cookie — load-bearing for this zone's DOCK: user-state is
// OIDC-only at the catalog, so a bearer-less write would 401 and the arrangement would silently
// never persist. Same session wiring as `makeZoneHooks`, without its telemetry handle.
export const handle = makeSessionHandle(makeOidcConfig(env));

// SSR reads (remote query() via getRequestEvent().fetch) issue relative /api/*,
// which in prod resolves against the external ingress origin and hairpins back
// through the ingress. Route them to the cluster edge's in-cluster address instead.
// Dev defaults to :8888; the chart sets RASK_EDGE_URL in-cluster.
// Client-side fetches are untouched. The rewrite is single-sourced in @rask/api
// so every app + every future endpoint inherits it — no per-call wiring.
export const handleFetch: HandleFetch = makeEdgeHandleFetch(edgeUrl(env));
