import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// On-demand GC (#75) — narrow session-only routes beside the GET-only /capi catch-all, with an explicit
// action allowlist (preview | run, never a blanket proxy). Forwards ONLY the signed-in user's bearer, so
// the catalog's owner-tier gate (can_drop) is enforced against a real user; an anonymous visitor on an OIDC
// tier is refused here.
const ACTIONS = new Set(['preview', 'run', 'compact']);

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (!ACTIONS.has(params.action)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to run table maintenance' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/maintenance/${params.action}`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi maintenance ${params.action} proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
