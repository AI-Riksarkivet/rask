import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

// `/api/projects` — the tenants panel's seam to the catalog's first-class projects API. Unlike the
// audit/jetstream routes (whose upstreams have no auth of their own), /v1/projects is estate-gated by
// the CATALOG's FGA (`can_observe_events` on the root object), so this BFF only bearer-forwards the
// signed-in user's token and passes the upstream verdict through — 401 anonymous on a governed stack,
// 403 for a non-estate-admin, the JSON body for an estate admin. No service-token fallback (the
// estate-wide tenant map must never be readable without a user identity).
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const GET: RequestHandler = async ({ fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view tenants' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	try {
		const upstream = await fetch(`${CATALOG_API}/v1/projects`, {
			headers,
			signal: AbortSignal.timeout(8000),
		});
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`api projects proxy upstream failure: ${String(err)}`);
		return json({ detail: 'catalog unreachable' }, { status: 502 });
	}
};
