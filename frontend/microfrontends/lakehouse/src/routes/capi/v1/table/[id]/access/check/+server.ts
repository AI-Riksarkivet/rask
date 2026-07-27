import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Access-check simulator (#68) — a narrow session-only route beside the GET-only /capi catch-all. Unlike
// access/list (body-less), the check carries a {user, relation} body, so it must be its OWN route (the
// catch-all would 405 a POST — which is exactly the bug the live Playwright route-liveness test caught).
// Same confused-deputy stance: forwards ONLY the signed-in user's bearer, so the catalog's owner-tier gate
// (can_drop) is enforced against a real user; an anonymous visitor on an OIDC web tier is refused here.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to simulate an access check' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/access/check`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi access-check proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
