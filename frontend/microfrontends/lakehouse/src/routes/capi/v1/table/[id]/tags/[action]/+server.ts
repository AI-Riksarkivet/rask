import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Tag mutation ops (#74) — narrow session-only routes (allowlist delete|update → the catalog's
// tags/{action}; create stays on the sibling tags/ route). Forwards ONLY the signed-in user's bearer, so
// the catalog's gate (owner-tier can_update_tag on update, writer-tier on delete) is enforced against a
// real user; an anonymous visitor on an OIDC tier is refused here.
const ACTIONS = new Set(['delete', 'update']);

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (!ACTIONS.has(params.action)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to manage tags' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/tags/${params.action}`;
	try {
		const res = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(res.body, {
			status: res.status,
			headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi tags ${params.action} proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
