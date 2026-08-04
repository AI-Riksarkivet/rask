import type { HandleFetch } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { makeGatewayHandleFetch } from '@rask/api';
import { makeOidcConfig, makeSessionHandle } from '@rask/api/bff';

// Session hydration from the sealed OIDC cookie — load-bearing for this zone's DOCK: user-state is
// OIDC-only at the catalog, so a bearer-less write would 401 and the arrangement would silently
// never persist. Deliberately NOT `makeZoneHooks(env, {gateway:true})`, which is the same handle
// paired with a handleFetch reading LANCE_GATEWAY_URL (the LINEAGE port, default :8001) — this zone
// reads the RAY plane through RASK_GATEWAY_URL, and local dev sets only that one. Same session
// wiring, this zone's own gateway.
export const handle = makeSessionHandle(makeOidcConfig(env));

// SSR reads (remote query() via getRequestEvent().fetch) issue relative /api/*,
// which in prod resolves against the external ingress origin and hairpins back
// through the ingress. Route them straight to the in-cluster gateway instead.
// Dev defaults to the local gateway; the chart sets RASK_GATEWAY_URL in-cluster.
// Client-side fetches are untouched. The rewrite is single-sourced in @rask/api
// so every app + every future endpoint inherits it — no per-call wiring.
export const handleFetch: HandleFetch = makeGatewayHandleFetch(
	env.RASK_GATEWAY_URL ?? 'http://localhost:8888',
);
