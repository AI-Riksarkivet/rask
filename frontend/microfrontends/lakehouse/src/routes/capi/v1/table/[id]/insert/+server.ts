import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Data-plane row insert (#64) — a narrow session-only route beside the GET-only /capi catch-all. Appends
// Arrow-IPC rows (the browser builds the stream with apache-arrow); writer-gated (can_write_data) at the
// catalog. Same confused-deputy stance as the policy/tag routes: forwards ONLY the signed-in user's bearer,
// so an anonymous visitor on an OIDC web tier is refused here without the request leaving the BFF. The
// binary body is streamed through; the `?mode=append` (and any other) query is forwarded verbatim.
export const POST: RequestHandler = async ({ params, request, url, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to insert rows' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/vnd.apache.arrow.stream',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/insert${url.search}`;
	try {
		const body = await request.arrayBuffer();
		const upstream = await fetch(target, { method: 'POST', headers, body });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi insert proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
