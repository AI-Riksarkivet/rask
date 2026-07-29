import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// NAMESPACE maintenance-policy writes (#50, sweep group 3) — narrow session-only routes beside the
// GET-only /capi catch-all, the same confused-deputy stance as the table policy routes: the write
// carries only the signed-in user's bearer, so the catalog's owner gate (can_delete on
// namespace:<id>) is enforced against a real user and an anonymous visitor on an OIDC-configured web
// tier is refused without the request leaving the BFF. PUT = set (idempotent replace), DELETE =
// remove (idempotent). A namespace policy governs every dataset under the namespace's prefix unless
// a table policy overrides it.
const write = (target: 'set' | 'delete'): RequestHandler => {
	return async ({ params, request, fetch, locals }) => {
		if (locals.authEnabled && !locals.session) {
			return json({ detail: 'sign in to edit the maintenance policy' }, { status: 401 });
		}
		const headers: Record<string, string> = {
			'content-type': request.headers.get('content-type') ?? 'application/json',
		};
		if (locals.session) {
			headers['authorization'] = `Bearer ${locals.session.accessToken}`;
		}
		const url = `${CATALOG_API}/v1/namespace/${encodeURIComponent(params.id)}/policy/${target}`;
		try {
			const upstream = await fetch(url, {
				method: 'POST',
				headers,
				body: target === 'set' ? await request.text() : undefined,
			});
			return new Response(upstream.body, {
				status: upstream.status,
				headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
			});
		} catch (err) {
			console.error(`capi namespace-policy proxy upstream failure: ${String(err)}`);
			return json({ detail: String(err) }, { status: 502 });
		}
	};
};

export const PUT = write('set');
export const DELETE = write('delete');
