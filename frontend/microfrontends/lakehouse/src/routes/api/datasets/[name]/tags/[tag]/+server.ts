import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

// Governance-tag writes (#49) — narrow, session-only routes beside the GET-only /api catch-all, the same
// confused-deputy stance as the /capi promote route: the write carries only the signed-in user's bearer,
// never the read-only service token, so the lineage writer rung (can_write_data) is enforced against a
// real user. In the built app SvelteKit's specific-segment precedence makes these win over [...path];
// every other /api write still 405s. An anonymous visitor on an OIDC-configured web tier is refused here
// without the request leaving the BFF; without OIDC configured the open dev lineage service decides.
const write = (method: 'PUT' | 'DELETE'): RequestHandler => {
	return async ({ params, fetch, locals }) => {
		if (locals.authEnabled && !locals.session) {
			return json({ detail: 'sign in to edit governance tags' }, { status: 401 });
		}
		const headers: Record<string, string> = {};
		if (locals.session) {
			headers['authorization'] = `Bearer ${locals.session.accessToken}`;
		}
		const target = `${LINEAGE_API}/datasets/${encodeURIComponent(params.name)}/tags/${encodeURIComponent(params.tag)}`;
		try {
			const upstream = await fetch(target, { method, headers });
			return new Response(upstream.body, {
				status: upstream.status,
				headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
			});
		} catch (err) {
			console.error(`lineage governance proxy upstream failure: ${String(err)}`);
			return json({ detail: 'lineage upstream unavailable' }, { status: 502 });
		}
	};
};

export const PUT = write('PUT');
export const DELETE = write('DELETE');
