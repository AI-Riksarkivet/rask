/**
 * The Model node's network lane: POST the upstream payload to a live Ray Serve
 * app through the zone's own `/api/infer` BFF (bytes ride a `+server.ts`, never
 * a remote function — the estate's transport ruling). The BFF validates the app
 * slug, forwards the body to `STUDIO_SERVE_URL/<app>`, and translates upstream
 * failures into `{reason, detail}` JSON — decoded here into a message the node
 * card can show.
 */
import { base } from '$app/paths';
import * as v from 'valibot';
import type { FlowPayload } from './types';
import type { InvokeRequest } from './executor';

/** Human message for the BFF's failure reasons (same vocabulary as the models
 *  zone's inference playground — the two proxies share one @rask/api helper). */
function failureMessage(
	status: number,
	reason: string | undefined,
	detail: string | undefined,
): string {
	// 413 has TWO possible authors and only one of them is ours. The BFF's guard answers JSON naming
	// the 25 MB ceiling; the Bun server's own `BODY_SIZE_LIMIT` answers an anonymous 413 BEFORE the
	// route runs, which is what a 512K default produced for a 2.25 MB scan. Say which, so the reader
	// is not left with a bare status: an upload past the SERVER limit is an env fix, not a smaller file.
	if (status === 413 && !detail) {
		return 'The upload is too large for the zone server (BODY_SIZE_LIMIT — 512K unless the deploy raises it). Rejected before the inference route ran.';
	}
	// A Serve app that EXISTS but exposes no HTTP ingress. Ray answers 500 with this exact text, not
	// the 405 the `wrong_serve_app` branch was written for — measured 2026-08-06 against the deployed
	// /htrflow, whose image predates the `__call__(Request)` handler and so offers only its
	// handle-only methods. Named because the fix is a Serve redeploy, and nothing about the message
	// otherwise points away from the browser.
	if (detail?.includes('does not exist. Available methods')) {
		return `That Serve app has no HTTP handler — its deployed image predates the ingress (${detail.slice(detail.indexOf('Available methods'), 120)}). Redeploy Serve.`;
	}
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
	const qs = new URLSearchParams({ app: req.app, name: req.name, path: req.path });
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
			// Parsed, not cast (#94): a non-string field degrades to the status line, never NaN-ish UI.
			const body = v.safeParse(
				v.looseObject({
					reason: v.optional(v.string()),
					detail: v.optional(v.string()),
					message: v.optional(v.string()),
				}),
				await res.json(),
			);
			if (body.success) {
				reason = body.output.reason;
				detail = body.output.detail ?? body.output.message;
			}
		} catch {
			// non-JSON failure body — the status alone will have to do
		}
		throw new Error(failureMessage(res.status, reason, detail));
	}
	const mime = res.headers.get('content-type') ?? 'text/plain';
	return { kind: 'text', text: await res.text(), mime, label: req.name };
}
