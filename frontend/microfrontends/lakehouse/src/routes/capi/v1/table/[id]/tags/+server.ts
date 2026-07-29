import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Tag-a-version write (#64 data-plane version management) — a narrow session-only route beside the
// GET-only /capi catch-all, the same confused-deputy stance as policy/promote/access: it forwards ONLY
// the signed-in user's bearer, so the catalog's writer gate (can_create_tag) is enforced against a real
// user and an anonymous visitor on an OIDC-configured web tier is refused without the request leaving
// the BFF. POST body {tag, version} → the catalog's tags/create.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to tag a version' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const url = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/tags/create`;
	try {
		const upstream = await fetch(url, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi tag proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
