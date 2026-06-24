// @rask/api/gateway — route SSR `/api/*` fetches to the in-cluster gateway.
//
// Under `svelte-adapter-bun` (prod), a server-side fetch of a RELATIVE `/api/*`
// URL resolves against the *incoming request's* origin — the EXTERNAL ingress
// host — so the pod hairpins out through the ingress and back in (and hard-fails
// when that origin isn't reachable from inside the cluster). This is a SvelteKit
// `handleFetch` factory: during SSR it rewrites same-origin `/api/*` requests to
// the in-cluster gateway base, so server reads go straight to the gateway.
// Client-side fetches are never touched (the browser's relative `/api` is
// same-origin and correct).
//
// Env-free ON PURPOSE: @rask/api is also imported in client bundles, so it must
// not import `$env/*/private`. The gateway URL is passed in from each app's
// (server-only) `hooks.server.ts` instead. Single-sourcing the rewrite here means
// every app — and every future @rask/api endpoint — inherits it with no per-call
// wiring (see docs/architecture/frontend-conventions.md §1).

/** The subset of SvelteKit's `handleFetch` input this factory reads. Kept
 *  structural so @rask/api stays dependency-light (no `@sveltejs/kit` import);
 *  the result is assignable to `HandleFetch`, which apps annotate it with. */
interface HandleFetchArgs {
	event: { url: URL };
	request: Request;
	fetch: typeof fetch;
}

/**
 * Build a `handleFetch` hook that routes SSR `/api/*` fetches to `gatewayUrl`.
 * Pass the in-cluster gateway base (`RASK_GATEWAY_URL`, default the dev gateway)
 * from a server-only module. A falsy `gatewayUrl` passes every request through
 * unchanged.
 */
export function makeGatewayHandleFetch(gatewayUrl: string | undefined) {
	const base = gatewayUrl?.replace(/\/$/, '');
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
