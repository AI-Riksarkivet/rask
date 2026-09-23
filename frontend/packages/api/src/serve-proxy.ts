// @rask/api/serve-proxy — the Ray Serve inference BFF body, shared by every zone that offers a
// "call a live Serve app" surface (server-only: it is mounted from a `+server.ts`).
//
// Unlike the rest of this package this module value-imports `error`/`json` from @sveltejs/kit rather
// than types only. That is deliberate and not incidental: the STATUS CODE is the payload here (see
// the failure taxonomy below), and kit's own `error()` answers `{ message }` — the exact shape the
// zones' clients already normalise against beside this helper's `{ reason }` problem bodies.
// Replicating either by hand would be a second, drifting definition of the same contract.
import { error, json, type RequestEvent } from '@sveltejs/kit';
import type { AuthLocals } from './bff';

/** Bound the upload the zone server will buffer. An inference input — an image, an audio clip, a
 *  JSON body — is at most a few MB; anything past this is a mistake or an attack, and rejecting it
 *  here keeps it out of the Bun server's memory. `chart/templates/frontends.yaml` sets the zone's
 *  `BODY_SIZE_LIMIT` above this on purpose, so this guard is the one that answers. */
const DEFAULT_MAX_BYTES = 25 * 1024 * 1024;

/** Model inference is seconds, not milliseconds, and the first call after a deploy pays the
 *  replica's cold start (weights loading) on top. */
const DEFAULT_TIMEOUT_MS = 180_000;

/** What the request and the answer are labelled when their sender did not say. The door carries
 *  bytes it does not interpret, so the only honest label for unlabelled bytes is "bytes". */
const UNTYPED = 'application/octet-stream';

export interface ServeProxyOptions {
	/** Full URL of the Serve app, e.g. `http://localhost:8000/<app>`. The zone owns the env var and
	 *  its default — that is the one thing that cannot be hoisted, since `$env/dynamic/private` is a
	 *  SvelteKit virtual module and this package stays env-free. */
	target: string;
	/** Upload ceiling in bytes. Default 25 MB. */
	maxBytes?: number;
	/** Upstream deadline in milliseconds. Default 180 000. */
	timeoutMs?: number;
}

/**
 * SERVE INFERENCE — request bytes in, response bytes out, both content-types passed through.
 *
 * The door interprets neither direction. Whatever the caller POSTs reaches the Serve app verbatim
 * under the caller's content-type, and whatever the app answers comes back verbatim under the app's
 * content-type. An image model, an audio model and a JSON-in/JSON-out LLM all take the same path;
 * what a body MEANS is the app's contract with its caller, never this helper's.
 *
 * Mounted from a `+server.ts` and never a remote function, by the estate's transport rule: this
 * carries BINARY both ways. devalue would base64 the upload inside the payload string (+33% on the
 * wire, triple-buffered, no streaming) and throw away the content-type and status semantics this
 * endpoint is built on — the difference between "Serve is down" and "this app has no HTTP ingress"
 * IS the status code here.
 *
 * ── WHY THIS DOES NOT GO THROUGH THE GATEWAY ────────────────────────────────────────────────────
 * It cannot, today. `/api/serve/*` looks like the right door and is not:
 *
 *   - the gateway (`services/gateway/src/gateway/__init__.py`, the `/api/serve` route row) sends
 *     `/api/serve` to the COMPUTE service, whose proxy (`services/compute/src/compute/proxy.py`)
 *     registers `_PROXY_METHODS = ["GET", "HEAD"]`. A POST is answered 405. That restriction is
 *     deliberate and documented beside it — Ray's dashboard exposes `PUT /api/serve/applications/`,
 *     which deploys arbitrary code, so that proxy stays read-only.
 *   - Even a GET would reach the wrong service: `ray-kit`'s dashboard client resolves against
 *     `RAY_DASHBOARD_URL` (`service-kit` config, `:8265`) — Ray's dashboard REST/status API.
 *     Inference lives on the Serve HTTP INGRESS, a different port speaking a different protocol.
 *     Nothing in the fleet routes to it.
 *
 * So this talks to the Serve ingress DIRECTLY. Routing inference through `/api/serve` is a real
 * backend change — a POST row on the compute proxy pointed at the Serve port, behind auth — not
 * something a zone may invent, so this does not pretend the door exists.
 *
 * ── THE UPSTREAM ────────────────────────────────────────────────────────────────────────────────
 * `opts.target` — `<Serve HTTP origin>/<route prefix>[<path within the app>]`. :8000 is Ray Serve's
 * default HTTP proxy port; a Serve application's `route_prefix` names it on that origin.
 *
 * An app answers HTTP only when its INGRESS deployment's `__call__` accepts a request. An app whose
 * deployments are reachable solely over a Serve DeploymentHandle is still "up" in Serve's status and
 * still answers a POST — with Serve's 405. That is the trap this reports on rather than swallows:
 * the honest first-run experience of pointing at such an app is a 405 from a Serve that is healthy.
 *
 * ── THE CONTRACT ────────────────────────────────────────────────────────────────────────────────
 * Thrown (kit `error()`, body `{ message }`): 401 signed out on a governed stack, 413 over the size
 * ceiling (declared header AND buffered fact), 400 on an empty body.
 * Returned problem JSON `{ reason, status?, detail, serveUrl }`: `upstream_unreachable` → 503,
 * `wrong_serve_app` → 501 (Serve answered 405), `upstream_error` → 502.
 * On 200: the upstream bytes under the upstream content-type (`application/octet-stream` when the
 * app sent none), `no-store`.
 * `?name=` on the caller's URL is forwarded as `?name=` when non-empty — an optional request label
 * for apps that read one; an app that does not simply ignores it.
 * Pinned by `serve-proxy.test.ts`.
 */
export async function proxyServeInfer(
	event: RequestEvent,
	opts: ServeProxyOptions,
): Promise<Response> {
	const { request, url } = event;
	// App.Locals is declared per-zone; in @rask/api's own typecheck it's empty, so read via AuthLocals.
	const locals = event.locals as unknown as AuthLocals;
	const serveUrl = opts.target;
	const maxBytes = opts.maxBytes ?? DEFAULT_MAX_BYTES;
	const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

	// Fail closed on a governed stack. Inference is real GPU work on a shared cluster, so an
	// anonymous caller must not be able to spend it — and hiding the control in the UI is
	// presentation, not authorization.
	if (locals.authEnabled && !locals.session) {
		error(401, 'sign in to run inference');
	}

	const tooLarge = `request body is larger than the ${Math.round(maxBytes / 1024 / 1024)} MB limit`;
	const declared = Number(request.headers.get('content-length') ?? '0');
	if (declared > maxBytes) error(413, tooLarge);

	const bytes = await request.arrayBuffer();
	if (bytes.byteLength === 0) error(400, 'empty request body');
	// content-length is a claim; the buffer is the fact.
	if (bytes.byteLength > maxBytes) error(413, tooLarge);

	const name = url.searchParams.get('name') ?? '';
	const target = name ? `${serveUrl}?name=${encodeURIComponent(name)}` : serveUrl;

	let res: Response;
	try {
		res = await fetch(target, {
			method: 'POST',
			headers: { 'content-type': request.headers.get('content-type') ?? UNTYPED },
			body: bytes,
			signal: AbortSignal.timeout(timeoutMs),
		});
	} catch (err) {
		// The honest degrade: no Serve on the other end. A JSON problem body rather than a thrown
		// boundary error, so the page can render "not wired" as a STATE instead of a crash.
		return json(
			{
				reason: 'upstream_unreachable',
				detail: String(err),
				serveUrl,
			},
			{ status: 503 },
		);
	}

	if (!res.ok) {
		const body = await res.text().catch(() => '');
		return json(
			{
				// 405 is the ONE misconfiguration worth naming: Serve is up, but the app at this route
				// has no HTTP ingress — its deployments are reachable only over a DeploymentHandle.
				reason: res.status === 405 ? 'wrong_serve_app' : 'upstream_error',
				status: res.status,
				detail: body.slice(0, 2000),
				serveUrl,
			},
			{ status: res.status === 405 ? 501 : 502 },
		);
	}

	// The app's bytes straight back to the browser, content-type intact — the whole reason this is a
	// route and not a remote function.
	return new Response(await res.arrayBuffer(), {
		status: 200,
		headers: {
			'content-type': res.headers.get('content-type') ?? UNTYPED,
			'cache-control': 'no-store',
		},
	});
}
