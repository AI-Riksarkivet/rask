import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Index create (#73) — a narrow session-only route beside the GET-only /capi catch-all. `?scalar=1`
// dispatches to the catalog's create_scalar_index (BTREE/BITMAP/INVERTED …); otherwise create_index (the
// IVF/HNSW vector families — the Lance differentiator neither Iceberg nor Unity has). Forwards ONLY the
// signed-in user's bearer, so the catalog's writer-tier gate (can_write_data) is enforced against a real
// user; an anonymous visitor on an OIDC web tier is refused here.
export const POST: RequestHandler = async ({ params, request, url, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to build an index' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const op = url.searchParams.get('scalar') === '1' ? 'create_scalar_index' : 'create_index';
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/${op}`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi index-create proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
