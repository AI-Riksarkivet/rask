import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Warehouse lifecycle writes (#3-A, UI round #52): activate / deactivate / namespaces (bind), each
// admin-gated by the catalog (can_administer on the warehouse's project). One narrow session-only
// route with an explicit action allowlist — never a blanket write proxy. GET /capi/v1/warehouses/{id}
// still flows through the generic catch-all (this route matches only two-segment paths).
const ACTIONS = new Set(['activate', 'deactivate', 'namespaces']);

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (!ACTIONS.has(params.action)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to administer warehouses' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/warehouses/${encodeURIComponent(params.id)}/${params.action}`;
	try {
		const body = await request.text();
		const upstream = await fetch(target, { method: 'POST', headers, body: body || undefined });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi warehouse action proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
