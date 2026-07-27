import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Restore-to-version write (#64 version management) — a narrow session-only route beside the GET-only
// /capi catch-all. Restore mints a FRESH version pointing at the restored data (never rewrites history),
// so it's owner-tier at the catalog (can_restore). Same confused-deputy stance as the policy/tag routes:
// forwards ONLY the signed-in user's bearer, and an anonymous visitor on an OIDC web tier is refused here.
export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to restore a version' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const url = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/restore`;
	try {
		const upstream = await fetch(url, { method: 'POST', headers, body: await request.text() });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi restore proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
