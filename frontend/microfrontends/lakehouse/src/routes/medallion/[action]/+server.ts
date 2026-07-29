import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const MEDALLION_API = env.MEDALLION_API ?? 'http://localhost:8000';

// #64 human trigger door: fire the medallion cascade head (/produce) or a training run (/train) from the
// UI. lance-ray's authorize_produce accepts EITHER the service app-token OR a signed-in project admin
// (OIDC bearer + can_administer). Like every other write route here (tags/promote/warehouse actions) this
// forwards ONLY the signed-in user's bearer — the service token never leaves the web pod — so the admin
// gate is enforced against a real user and an anonymous visitor on an OIDC tier is refused at the BFF.
// One narrow route with an explicit allowlist, never a blanket proxy.
const ACTIONS = new Set(['produce', 'train']);

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (!ACTIONS.has(params.action)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to trigger the pipeline' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	// The trigger's 503+Retry-After idempotency contract only pairs a retry with its first attempt if the
	// key rides along — forward it when the caller sends one.
	const idem = request.headers.get('idempotency-key');
	if (idem) {
		headers['idempotency-key'] = idem;
	}
	const target = `${MEDALLION_API}/${params.action}`;
	try {
		const body = await request.text();
		const upstream = await fetch(target, { method: 'POST', headers, body: body || undefined });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`medallion ${params.action} proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
