import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Drop a namespace (#85 lifecycle) — a narrow session-only write route beside the GET-only /capi
// catch-all (which would 405 this POST). Same confused-deputy stance as access/grant: it forwards
// ONLY the signed-in user's bearer, so the catalog's owner-tier gate (can_delete on namespace:<id>)
// is enforced against a real user and an anonymous visitor on an OIDC web tier is refused here
// without leaving the BFF. Cascade travels in the body (`behavior: "Cascade"`), passed through as-is.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to drop a namespace' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/namespace/${encodeURIComponent(params.id)}/drop`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi namespace-drop proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
