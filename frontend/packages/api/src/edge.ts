// @rask/api/edge — address `/api/*` from a zone's server through the cluster's edge.
//
// The edge is the platform's Gateway: Gateway API HTTPRoutes the chart renders map each public
// `/api/*` prefix to the service that owns it, and every service authenticates the request itself.
// A zone's server reaches `/api/*` THROUGH that route, at `RASK_EDGE_URL` (in-cluster, the Gateway's
// in-cluster address; in dev, :8888, which `make dev-frontends-k3s` port-forwards to the cluster's
// Gateway), so the public route table is the only route table and no zone restates which service owns
// a path.
//
// Under `svelte-adapter-bun` (prod), a server-side fetch of a RELATIVE `/api/*` URL resolves against
// the *incoming request's* origin — the EXTERNAL host — so the pod hairpins out and back in (and
// hard-fails when that origin is not reachable from inside the cluster). `makeEdgeHandleFetch` is a
// SvelteKit `handleFetch` factory: during SSR it rewrites same-origin `/api/*` requests onto the edge
// base. Client-side fetches are never touched (the browser's relative `/api` is same-origin and
// correct).
//
// Env-free ON PURPOSE: @rask/api is also imported in client bundles, so it must not import
// `$env/*/private`. Each zone passes its (server-only) env in.

/** The edge in local dev: the cluster's Gateway port-forwarded to :8888. The chart sets `RASK_EDGE_URL` in-cluster. */
export const DEFAULT_EDGE_URL = 'http://localhost:8888';

/** The edge base a zone's server addresses `/api/*` on, trailing slash trimmed. */
export function edgeUrl(env: Record<string, string | undefined>): string {
	return (env.RASK_EDGE_URL ?? DEFAULT_EDGE_URL).replace(/\/$/, '');
}

/** The subset of SvelteKit's `handleFetch` input this factory reads. Kept
 *  structural so @rask/api stays dependency-light (no `@sveltejs/kit` import);
 *  the result is assignable to `HandleFetch`, which apps annotate it with. */
interface HandleFetchArgs {
	event: { url: URL };
	request: Request;
	fetch: typeof fetch;
}

/**
 * Build a `handleFetch` hook that routes SSR same-origin `/api/*` fetches to `edgeBase`. A falsy
 * `edgeBase` passes every request through unchanged.
 */
export function makeEdgeHandleFetch(edgeBase: string | undefined) {
	const base = edgeBase?.replace(/\/$/, '');
	return ({ event, request, fetch }: HandleFetchArgs): Promise<Response> => {
		if (base) {
			const origin = event.url.origin;
			if (request.url.startsWith(`${origin}/api/`)) {
				return fetch(new Request(base + request.url.slice(origin.length), request));
			}
		}
		return fetch(request);
	};
}
