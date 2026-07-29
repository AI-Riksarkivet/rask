import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

// The description write (#49) — same narrow session-only shape as the sibling tag routes: the signed-in
// user's bearer only (never the read-only service token), 401 short-circuit when the web tier has OIDC
// configured but no session, upstream status passed through verbatim.
export const PUT: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to edit the description' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${LINEAGE_API}/datasets/${encodeURIComponent(params.name)}/description`;
	try {
		const upstream = await fetch(target, { method: 'PUT', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`lineage governance proxy upstream failure: ${String(err)}`);
		return json({ detail: 'lineage upstream unavailable' }, { status: 502 });
	}
};
