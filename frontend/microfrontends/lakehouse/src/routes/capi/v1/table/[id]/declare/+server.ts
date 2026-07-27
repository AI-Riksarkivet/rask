import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Declare an empty table (#85 registry create) — the one browser-shaped create path (JSON body, no
// Arrow payload; the catalog seeds the caller's FGA ownership and emits the DECLARE_TABLE marker). A
// narrow session-only route beside the GET-only /capi catch-all: forwards ONLY the signed-in user's
// bearer so the catalog's can_create_table gate on the parent namespace is enforced against a real
// user; an anonymous visitor on an OIDC web tier is refused here.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to declare a table' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/declare`;
	try {
		const upstream = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi table-declare proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
