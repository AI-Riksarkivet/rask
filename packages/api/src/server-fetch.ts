/**
 * Server-fetch helpers for @rask/api — framework-agnostic, no @sveltejs/kit import.
 *
 * `rewriteApiToGateway` is intended for use in a SvelteKit `handleFetch` hook:
 * it transparently rewrites server-side `/api/*` requests to the absolute
 * in-cluster gateway URL so SSR `query()` calls that pass `getRequestEvent().fetch`
 * reach the gateway instead of trying to connect to the pod's own origin.
 */

/**
 * If `request.url` has a `/api/` pathname prefix, return a new `Request` whose
 * URL is resolved against `gatewayBase`; otherwise return `request` unchanged.
 *
 * @param request    The incoming fetch request (from `handleFetch`'s `{ request }`).
 * @param gatewayBase  Absolute base URL of the gateway, e.g. `http://rask-gateway:8888`.
 */
export function rewriteApiToGateway(request: Request, gatewayBase: string): Request {
	const url = new URL(request.url);
	if (!url.pathname.startsWith('/api/')) {
		return request;
	}
	const rewritten = new URL(url.pathname + url.search, gatewayBase);
	return new Request(rewritten, request);
}
