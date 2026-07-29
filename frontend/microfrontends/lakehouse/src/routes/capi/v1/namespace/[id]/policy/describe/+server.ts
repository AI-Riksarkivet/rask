import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// NAMESPACE maintenance-policy describe (#50, sweep group 3) — reader-tier, but the catalog's
// lance-namespace-style surface is POST-shaped so the GET-only /capi catch-all would 405 it; hence
// its own narrow session-only route (the table equivalent rides the /detail aggregate instead).
// Same confused-deputy stance as the other named routes: forwards ONLY the signed-in user's bearer;
// an anonymous visitor on an OIDC web tier is refused here. 404 = no policy set (an honest state,
// surfaced as-is).
export const POST: RequestHandler = async ({ params, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view the maintenance policy' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/namespace/${encodeURIComponent(params.id)}/policy/describe`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi namespace-policy-describe proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
