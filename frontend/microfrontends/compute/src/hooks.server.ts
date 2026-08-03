import type { HandleFetch } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { makeGatewayHandleFetch } from '@rask/api';

// SSR reads (remote query() via getRequestEvent().fetch) issue relative /api/*,
// which in prod resolves against the external ingress origin and hairpins back
// through the ingress. Route them straight to the in-cluster gateway instead.
// Dev defaults to the local gateway; the chart sets RASK_GATEWAY_URL in-cluster.
// Client-side fetches are untouched. The rewrite is single-sourced in @rask/api
// so every app + every future endpoint inherits it — no per-call wiring.
export const handleFetch: HandleFetch = makeGatewayHandleFetch(
	env.RASK_GATEWAY_URL ?? 'http://localhost:8888',
);
