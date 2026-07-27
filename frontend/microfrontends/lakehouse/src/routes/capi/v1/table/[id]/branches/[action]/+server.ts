import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Branch ops (#74) — narrow session-only routes beside the GET-only /capi catch-all (allowlist
// create|delete → the catalog's branches/{action}). Forwards ONLY the signed-in user's bearer, so the
// catalog's gate (owner-tier can_create_branch on create, writer-tier on delete) is enforced against a real
// user; an anonymous visitor on an OIDC tier is refused here.
const ACTIONS = new Set(['create', 'delete']);

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (!ACTIONS.has(params.action)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to manage branches' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/branches/${params.action}`;
	try {
		const res = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(res.body, {
			status: res.status,
			headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi branches ${params.action} proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
