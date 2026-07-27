import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Authorization graph around a NAMESPACE (#81, sweep group 3) — a narrow session-only route beside the
// GET-only /capi catch-all (which would 405 this POST). Forwards ONLY the signed-in user's bearer, so
// the catalog's owner-tier gate (can_delete on namespace:<id>) is enforced against a real user and an
// anonymous visitor on an OIDC web tier is refused here.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view the authorization graph' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/namespace/${encodeURIComponent(params.id)}/access/graph`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi namespace-access-graph proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
