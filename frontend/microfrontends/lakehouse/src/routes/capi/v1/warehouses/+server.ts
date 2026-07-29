import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Warehouse admin (#3-A, UI round #52). This specific route shadows the /capi catch-all for the
// collection path, so it must serve GET (list) itself — same session-only forwarding the catch-all
// would have done — alongside the narrow POST (create). The create carries only the signed-in
// user's bearer: the catalog's project-admin gate (can_create_warehouse) is enforced against a real
// user, never a service credential (the promote-route confused-deputy stance).
const forward = async (
	fetchFn: typeof fetch,
	session: { accessToken: string } | null,
	init: { method: 'GET' | 'POST'; body?: string; contentType?: string },
): Promise<Response> => {
	const headers: Record<string, string> = {};
	if (init.contentType) headers['content-type'] = init.contentType;
	if (session) headers['authorization'] = `Bearer ${session.accessToken}`;
	try {
		const upstream = await fetchFn(`${CATALOG_API}/v1/warehouses`, {
			method: init.method,
			headers,
			body: init.body,
		});
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi warehouses proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};

export const GET: RequestHandler = async ({ fetch, locals }) => {
	return forward(fetch, locals.session, { method: 'GET' });
};

export const POST: RequestHandler = async ({ request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to provision a warehouse' }, { status: 401 });
	}
	return forward(fetch, locals.session, {
		method: 'POST',
		body: await request.text(),
		contentType: request.headers.get('content-type') ?? 'application/json',
	});
};
