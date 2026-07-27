import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Access review on a NAMESPACE (#51, sweep group 3) — a narrow session-only route beside the GET-only
// /capi catch-all, the same confused-deputy stance as the table access/list route: the review carries
// only the signed-in user's bearer (never a service credential), so the catalog's owner-tier gate
// (can_delete on namespace:<id>) is enforced against a real user, and an anonymous visitor on an
// OIDC-configured web tier is refused here without the request leaving the BFF. POST because the
// catalog's lance-namespace-style surface is POST-shaped; the operation itself is a read.
export const POST: RequestHandler = async ({ params, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to review access' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/namespace/${encodeURIComponent(params.id)}/access/list`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi namespace-access proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
