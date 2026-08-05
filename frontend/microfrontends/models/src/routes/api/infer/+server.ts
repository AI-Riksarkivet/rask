import { error, json } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

/**
 * HTR INFERENCE — raw image bytes in, ALTO XML out.
 *
 * A `+server.ts` and not a remote function, by the estate's transport rule: this carries BINARY both
 * ways (an uploaded page image up, an XML document down). devalue would base64 the upload inside the
 * payload string (+33% on the wire, triple-buffered, no streaming) and throw away the content-type
 * and status semantics this endpoint is built on — the difference between "Serve is down" and "the
 * wrong Serve app is deployed" IS the status code here.
 *
 * ── WHY THIS DOES NOT GO THROUGH THE GATEWAY ────────────────────────────────────────────────────
 * It cannot, today. `/api/serve/*` looks like the right door and is not:
 *
 *   - `services/gateway/src/gateway/__init__.py:124` routes `/api/serve` to the COMPUTE service, and
 *     `services/compute/src/compute/proxy.py:22` registers it with `_PROXY_METHODS = ["GET","HEAD"]`.
 *     A POST is answered 405. That restriction is deliberate and documented at `proxy.py:16-21` —
 *     Ray's dashboard exposes `PUT /api/serve/applications/`, which deploys arbitrary code, so the
 *     proxy is read-only until an auth layer sits in front of `/api`.
 *   - Even a GET would reach the wrong service: `packages/ray-kit/src/ray_kit/dashboard.py:528`
 *     resolves against `RAY_DASHBOARD_URL` (`service-kit/config.py:29`, `:8265`) — Ray's dashboard
 *     REST/status API. Inference lives on the Serve HTTP INGRESS at `:8000`, a different port
 *     speaking a different protocol. Nothing in the fleet routes to it.
 *
 * So this endpoint talks to the Serve ingress DIRECTLY, exactly as the only other production consumer
 * does (`services/medallion/.../htr_transcribe.py:38-42` posts image bytes to `MEDALLION_HTRFLOW_URL`
 * and also bypasses the gateway). Routing inference through `/api/serve` is a real backend change —
 * a POST row on the compute proxy pointed at the Serve port, behind auth — not something a zone may
 * invent, so this does not pretend the door exists.
 *
 * ── THE UPSTREAM ────────────────────────────────────────────────────────────────────────────────
 * `MODELS_SERVE_URL`, default `http://localhost:8000/htrflow`.
 *
 * :8000 is where Ray Serve binds (`runners/htr/scripts/deploy_serve.py:74`, and the Makefile says so
 * out loud at :323 — "Port 8001, not 8000: Ray Serve's HTTP proxy holds :8000"). `/htrflow` is the
 * route prefix (`deploy_serve.py:44-47`; in-cluster `chart/values.yaml` `ray.serveRoutePrefix`).
 *
 * `/htrflow` is the ONLY HTR deployment that answers HTTP: its `__call__` takes a Starlette Request
 * (`runners/htr/src/runner/htrflow_service.py:221-248`) — raw bytes in the body, no JSON envelope, no
 * multipart, optional `?name=`, ALTO XML back. `/transcribe` takes `list[Image.Image]`
 * (`transcribe_service.py:180-182`), so it is reachable only over a Serve DeploymentHandle and
 * answers an HTTP POST with Serve's default 405.
 *
 * That last fact is the trap this endpoint reports on rather than swallows: plain `make serve-up`
 * deploys `transcribe` ONLY (`deploy_serve.py:112-117`), so the honest first-run experience is a 405
 * from a Serve that is up. `make serve-up-htrflow` (or `serve-up-both`) is what makes this work.
 */

const SERVE_URL = env.MODELS_SERVE_URL ?? 'http://localhost:8000/htrflow';

/** Bound the upload the zone server will buffer. A page scan is a few MB; anything past this is a
 *  mistake or an attack, and rejecting it here keeps it out of the Bun server's memory. */
const MAX_BYTES = 25 * 1024 * 1024;

/** Serve loads a model and runs layout + line detection + transcription per page — seconds, not
 *  milliseconds, and the first call after a deploy pays cold-start on top. */
const TIMEOUT_MS = 180_000;

export const POST: RequestHandler = async ({ request, locals, url }) => {
	// Fail closed on a governed stack. Inference is real GPU work on a shared cluster, so an
	// anonymous caller must not be able to spend it — and hiding the control in the UI is
	// presentation, not authorization.
	if (locals.authEnabled && !locals.session) {
		error(401, 'sign in to run inference');
	}

	const declared = Number(request.headers.get('content-length') ?? '0');
	if (declared > MAX_BYTES) {
		error(413, `image is larger than the ${Math.round(MAX_BYTES / 1024 / 1024)} MB limit`);
	}

	const bytes = await request.arrayBuffer();
	if (bytes.byteLength === 0) error(400, 'no image bytes in the request body');
	// content-length is a claim; the buffer is the fact.
	if (bytes.byteLength > MAX_BYTES) {
		error(413, `image is larger than the ${Math.round(MAX_BYTES / 1024 / 1024)} MB limit`);
	}

	// `?name=` rides through to Serve, which stamps it as the page identifier inside the ALTO.
	const name = url.searchParams.get('name') ?? '';
	const target = name ? `${SERVE_URL}?name=${encodeURIComponent(name)}` : SERVE_URL;

	let res: Response;
	try {
		res = await fetch(target, {
			method: 'POST',
			// Serve reads the raw body; the content-type is passed through for fidelity but htrflow
			// does not branch on it (`htrflow_service.py:221-248` decodes the bytes directly).
			headers: {
				'content-type': request.headers.get('content-type') ?? 'application/octet-stream',
			},
			body: bytes,
			signal: AbortSignal.timeout(TIMEOUT_MS),
		});
	} catch (err) {
		// The honest degrade: no Serve on the other end. A JSON problem body rather than a thrown
		// boundary error, so the page can render "not wired" as a STATE instead of a crash.
		return json(
			{
				reason: 'upstream_unreachable',
				detail: String(err),
				serveUrl: SERVE_URL,
			},
			{ status: 503 },
		);
	}

	if (!res.ok) {
		const body = await res.text().catch(() => '');
		return json(
			{
				// 405 is the ONE misconfiguration worth naming: Serve is up, but the app deployed at this
				// route has no HTTP handler — i.e. `make serve-up` deployed `transcribe` and not `htrflow`.
				reason: res.status === 405 ? 'wrong_serve_app' : 'upstream_error',
				status: res.status,
				detail: body.slice(0, 2000),
				serveUrl: SERVE_URL,
			},
			{ status: res.status === 405 ? 501 : 502 },
		);
	}

	// ALTO XML straight back to the browser, content-type intact — the whole reason this is a route
	// and not a remote function.
	return new Response(await res.arrayBuffer(), {
		status: 200,
		headers: {
			'content-type': res.headers.get('content-type') ?? 'application/xml',
			'cache-control': 'no-store',
		},
	});
};
