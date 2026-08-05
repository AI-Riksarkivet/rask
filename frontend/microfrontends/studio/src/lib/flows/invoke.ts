/**
 * The Model node's network lane: POST the upstream payload to a live Ray Serve
 * app through the zone's own `/api/infer` BFF (bytes ride a `+server.ts`, never
 * a remote function — the estate's transport ruling). The BFF validates the app
 * slug, forwards the body to `STUDIO_SERVE_URL/<app>`, and translates upstream
 * failures into `{reason, detail}` JSON — decoded here into a message the node
 * card can show.
 */
import { base } from '$app/paths';
import type { FlowPayload } from './types';
import type { InvokeRequest } from './executor';

/** Human message for the BFF's failure reasons (same vocabulary as the models
 *  zone's inference playground — the two proxies share one @rask/api helper). */
function failureMessage(
	status: number,
	reason: string | undefined,
	detail: string | undefined,
): string {
	switch (reason) {
		case 'upstream_unreachable':
			return 'Ray Serve is unreachable — is `make serve-up-htrflow` running?';
		case 'wrong_serve_app':
			return 'That Serve app is deployed without an HTTP handler (405) — pick an ingress app like htrflow.';
		case 'upstream_error':
			return detail || 'The Serve app returned an error.';
		default:
			return detail || `Inference failed (HTTP ${status}).`;
	}
}

/** POST `req.payload` to Serve app `req.app`; resolves to the response as a
 *  text payload (htrflow answers ALTO XML). Throws with a human message on any
 *  failure — the executor records it on the node. */
export async function invokeServe(req: InvokeRequest): Promise<FlowPayload> {
	const qs = new URLSearchParams({ app: req.app, name: req.name });
	const res = await fetch(`${base}/api/infer?${qs}`, {
		method: 'POST',
		headers: { 'content-type': req.payload.mime },
		body: req.payload.kind === 'bytes' ? req.payload.bytes : req.payload.text,
	});
	if (!res.ok) {
		let reason: string | undefined;
		let detail: string | undefined;
		try {
			// Two failure shapes, both load-bearing (see @rask/api/serve-proxy): thrown
			// kit error()s (401/413/400) answer `{ message }`, upstream problems answer
			// `{ reason, detail }`. Normalise here or a 401 renders as a bare status.
			const body = (await res.json()) as { reason?: string; detail?: string; message?: string };
			reason = body.reason;
			detail = body.detail ?? body.message;
		} catch {
			// non-JSON failure body — the status alone will have to do
		}
		throw new Error(failureMessage(res.status, reason, detail));
	}
	const mime = res.headers.get('content-type') ?? 'text/plain';
	return { kind: 'text', text: await res.text(), mime, label: req.name };
}
