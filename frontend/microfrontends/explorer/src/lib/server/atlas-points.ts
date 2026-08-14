/**
 * The server half of the atlas-points fix: `GET ${base}/api/atlas/points`, cached in THIS zone's BFF.
 *
 * The measurement, taken 2026-07-26 through the ingress on this cluster:
 *
 *   $ curl -s -o /dev/null -w '%{http_code} %{size_download}\n' \
 *       'http://localhost:8090/explorer/api/atlas/points?space=text&v=6'
 *   200 6678928
 *
 * `UrlMemo` in `@rask/explorer-api` already stops the same tab paying that twice, which is what the
 * Text/Visual toggle used to do. It cannot help a refresh, a second tab or a second user, and a burst of
 * concurrent multi-megabyte reads is what OOM-killed the viewer (`exit 137`) and took the media plane to
 * 502. So this route sits in front of the same upstream the `/api/[...path]` catch-all would have used
 * and adds the half that lives in the server, on {@link SharedPayloadCache}: authorize every request,
 * key on the resource, single-flight.
 *
 * ## The authorization, and what it is worth today
 *
 * That same curl is the whole problem statement for the security half: it carried **no session and no
 * bearer**, and it was answered with the full projection. `makeViewerProxy` forwards a bearer when there
 * is one and shrugs when there is not, and the viewer service registers no authentication at all
 * (`services/common/core/middleware.py` adds CORS and deliberately nothing else). So before this route
 * existed, `/explorer/api/atlas/points` was an open read for anyone who could reach the ingress — and a
 * cache in front of an unauthorized read would have made a fast open read out of a slow one.
 *
 * Two checks therefore run on EVERY call, before any cached byte is reachable:
 *
 * 1. **A session, fail-closed.** On an auth-enabled stack a caller with no session gets 401 and the
 *    request never leaves the BFF. This is the same stance `makeBackendProxy({ requireSession: true })`
 *    takes on the write routes; the difference is only that a 6.6 MB dataset-wide projection is not a
 *    read this estate should hand to an anonymous socket.
 * 2. **An authorization probe against the same resource, with the caller's own credential.** `GET
 *    /api/atlas/status?space=&dataset=` is the cheap sibling of `/points`: same dataset handle, same
 *    space resolution, ~25 ms and 99 bytes against the 6.6 MB it guards. Its verdict is used once and
 *    discarded — {@link SharedPayloadCache} caches the payload and never the decision. Today the viewer
 *    answers it for any caller, so the probe's real value is structural: the day the media corpus moves
 *    onto the governed warehouse (`#103`) and the viewer starts FGA-checking the dataset, this route
 *    withholds warm bytes from a refused caller with no further change. It is wired now, and tested now,
 *    because retrofitting an authorization check onto a warm cache is the shape of bug that ships.
 *
 * ## The store: in-process, per replica — and what that costs
 *
 * `chart/values-prod.yaml` sets `frontend.replicas: 2` with no session affinity, so two users can land on
 * different zone pods. What an in-process cache means there, precisely: the projection is materialised at
 * most **once per replica per freshness window**, not once per user. With two replicas the estate pays 2
 * cold fills per 5 minutes instead of one per mount, every subsequent user on either pod is warm, and the
 * concurrency that actually killed the viewer is collapsed on each pod by single-flight.
 *
 * A shared store would buy the difference between 2 cold fills and 1, and it would cost a 6.6 MB
 * round-trip on every hit — replacing an in-cluster read from the viewer with an in-cluster read from
 * Redis, plus a dependency and a failure mode this zone does not have today. That is not a trade worth
 * making at N=2. It becomes worth revisiting if replicas grow (the cold-fill count is linear in N) or if
 * the viewer's 1536Mi tier is squeezed again; the right answer then is an HTTP cache in the edge that
 * already sees `Cache-Control: public, max-age=300` on this response (nginx `proxy_cache`), not a Redis
 * holding multi-megabyte blobs — the payload is already an immutable, versioned, dataset-wide HTTP body,
 * which is precisely what an HTTP cache is for. Either way it is a chart change, and chart/ is not this
 * scope's to edit.
 */
import type { RequestHandler } from '@sveltejs/kit';
import type { AuthLocals } from '@rask/api/bff';
import { SharedPayloadCache } from '@rask/explorer-api/server-cache';

type Env = Record<string, string | undefined>;

/** Dev topology default, matching `makeViewerProxy`. In-cluster the chart sets VIEWER_API explicitly. */
const DEFAULT_VIEWER_API = 'http://localhost:8101';

/**
 * Retention budget. The corpus declares three atlas spaces (text/visual/caption) at ~6.6 MB each, so 32
 * MiB holds every space of every dataset a zone realistically serves, with headroom. The real ceiling is
 * the zones' OWN tier — `frontend.resources.limits.memory: 256Mi` (chart/values.yaml), NOT the fleet's
 * 512Mi default this comment used to cite (#141) — so this cache may claim at most ~1/8th of the pod,
 * shared with the Bun SSR heap. Raise the pod tier before raising this budget.
 */
const DEFAULT_MAX_BYTES = 32 * 1024 * 1024;

/**
 * The route's freshness ceiling. The resource is versioned — the client's `v` token is in the key, and
 * the viewer memoises the same payload on `(dataset, space, table version)` — but a table rewritten under
 * an unchanged `v` would otherwise strand a stale projection here for ever. Five minutes is the same
 * window the viewer already publishes as `Cache-Control: public, max-age=300`, so this ceiling and the
 * upstream's own claim agree; whichever is stricter wins.
 */
const MAX_AGE_MS = 300_000;

/**
 * The query parameters that identify the RESOURCE. Everything else a caller sends is dropped — not just
 * from the key but from the upstream URL too, so the key and the bytes it names can never disagree. The
 * list is deliberately closed: an open key would let `?foo=1` fork the cache, and a key built from the
 * raw query string would let it be forked without bound.
 */
const RESOURCE_PARAMS = ['dataset', 'space'] as const;

/**
 * `v` is deliberately NOT here, and the omission is the fix for a real hole rather than an oversight.
 *
 * It is a caller-supplied, unvalidated token. While it was part of the key, any signed-in user could mint
 * unbounded distinct keys — each one a full 6.6 MB upstream read and a retained 6.6 MB entry. Measured
 * against the live estate: six junk tokens evicted the product entry (`x-cache: hit` → `miss`), and twenty
 * tokens from ONE session added twenty reads to the viewer's own access log — 133 MB on demand, the shared
 * entry permanently cold, and single-flight defeated because every key was unique. That is precisely the
 * burst of concurrent multi-megabyte reads that OOM-killed the viewer in the first place.
 *
 * Dropping it is safe because the upstream does not read it: `services/viewer/api/v1/endpoints/atlas.py`
 * takes `space` and `dataset` only. `v` exists for the BROWSER memo, where a bump must make old entries
 * unreachable in a tab; here the freshness bound is MAX_AGE_MS, which does the same job without letting a
 * caller choose the key.
 *
 * The earlier test asserted that `?cachebust=` and `?user=` were dropped, so cache-forking read as closed
 * while the one parameter that was kept was the unbounded one. A test that proves the good cases and skips
 * the live one is worse than no test, because it is believed.
 */

/** The canonical, sorted resource query — the same for two callers who spelled it in a different order. */
function resourceQuery(url: URL): URLSearchParams {
	const out = new URLSearchParams();
	for (const name of [...RESOURCE_PARAMS].sort()) {
		const value = url.searchParams.get(name);
		if (value !== null && value !== '') out.set(name, value);
	}
	return out;
}

/**
 * The cache key: the dataset, the space and the payload version. Never the subject — no session, no
 * bearer, no cookie — which is what lets one entry serve everyone allowed to see it, and what makes
 * invalidation free (a new build bumps `v`, and every old key becomes unreachable at once).
 */
export function resourceKey(url: URL): string {
	return `atlas/points?${resourceQuery(url).toString()}`;
}

const json = (body: unknown, status: number): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/**
 * Build the route. Returns the cache alongside the handler so tests can assert on upstream call counts
 * and retention rather than infer them; `+server.ts` takes only the handler.
 */
export function makeAtlasPointsRoute(env: Env): { GET: RequestHandler; cache: SharedPayloadCache } {
	const viewerApi = env.VIEWER_API ?? DEFAULT_VIEWER_API;
	const maxBytes = Number(env.ATLAS_CACHE_MAX_BYTES ?? DEFAULT_MAX_BYTES);
	const cache = new SharedPayloadCache({
		maxBytes: Number.isFinite(maxBytes) && maxBytes > 0 ? maxBytes : DEFAULT_MAX_BYTES,
	});

	const GET: RequestHandler = ({ url, fetch, locals }) => {
		const auth = locals as unknown as AuthLocals;
		const query = resourceQuery(url);
		const headers: Record<string, string> = {};
		if (auth.session) headers['authorization'] = `Bearer ${auth.session.accessToken}`;

		return cache.serve(resourceKey(url), {
			maxAgeMs: MAX_AGE_MS,
			authorize: async () => {
				if (auth.authEnabled && !auth.session) {
					return json({ detail: 'sign in required' }, 401);
				}
				// The probe names the same dataset and space as the payload; `v` is the client's schema
				// token and means nothing upstream.
				const probeQuery = new URLSearchParams(query);
				probeQuery.delete('v');
				let probe: Response;
				try {
					probe = await fetch(`${viewerApi}/api/atlas/status?${probeQuery.toString()}`, {
						headers,
					});
				} catch (err) {
					// The upstream is unreachable, so nobody can be authorized right now. Fail closed:
					// serving a warm entry here would be a cache that outlives its own authorization.
					return json({ error: String(err) }, 502);
				}
				if (probe.ok) return null;
				return new Response(await probe.text(), {
					status: probe.status,
					headers: {
						'content-type': probe.headers.get('content-type') ?? 'application/json',
					},
				});
			},
			// A transport failure becomes a 502 Response rather than a rejection, matching what the
			// `/api/[...path]` catch-all returns for the same case: the browser client parses an error
			// BODY, and a rejection here would surface as the zone's generic 500 page instead. A 502 is
			// non-2xx, so it is never retained and never shared.
			load: async () => {
				try {
					return await fetch(`${viewerApi}/api/atlas/points?${query.toString()}`, { headers });
				} catch (err) {
					return json({ error: String(err) }, 502);
				}
			},
		});
	};

	return { GET, cache };
}

/** The handler alone — what `+server.ts` exports as `GET`. */
export const makeAtlasPointsHandler = (env: Env): RequestHandler => makeAtlasPointsRoute(env).GET;
