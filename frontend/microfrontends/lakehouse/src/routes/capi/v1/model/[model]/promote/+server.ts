import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// The one catalog write the UI performs, deliberately its own narrow route instead of a POST on the
// generic /capi proxy: a promote carries only the signed-in user's bearer — never any service
// credential — so the catalog's validator rung (can_promote) is enforced against a real user, and an
// anonymous visitor is refused here without the request ever leaving the BFF (the same confused-deputy
// stance that keeps the /api lineage proxy GET-only, bug hunt 2026-07-13).
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	// The catalog is the enforcement point (validator rung); this gate is the UX short-circuit for a
	// web tier that HAS OIDC configured but no session. Without OIDC configured (auth-off dev, or the
	// governed kind stack whose Dex is not browser-reachable) the request goes through unauthenticated
	// and the catalog decides — open stack: accepted; governed stack: its own 401.
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to promote a model' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/model/${encodeURIComponent(params.model)}/promote`;
	try {
		const upstream = await fetch(target, {
			method: 'POST',
			headers,
			body: await request.text(),
		});
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
