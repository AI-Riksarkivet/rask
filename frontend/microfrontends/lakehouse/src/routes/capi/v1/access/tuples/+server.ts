import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import { makeCatalogProxy } from '@rask/api/bff';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// FGA tuple writes on the catalog's estate-admin access surface (/v1/access/tuples) — the narrow
// session-only routes beside the GET-only /capi catch-all (which already proxies the tuple LIST).
// The body is ENUMERATED, never streamed: exactly {user, relation, object}, all non-empty strings,
// rebuilt here — so the session-bearing proxy can only ever issue the one write shape the catalog's
// estate-admin gate (the /v1/events bar) then authorizes. POST writes a tuple; DELETE removes one.
const forward = async (
	fetchFn: typeof fetch,
	session: { accessToken: string } | null,
	method: 'POST' | 'DELETE',
	raw: unknown,
): Promise<Response> => {
	const body = (raw ?? {}) as Record<string, unknown>;
	const user = body.user;
	const relation = body.relation;
	const object = body.object;
	if (
		typeof user !== 'string' ||
		typeof relation !== 'string' ||
		typeof object !== 'string' ||
		!user ||
		!relation ||
		!object
	) {
		return json({ detail: 'a tuple is {user, relation, object}, all strings' }, { status: 422 });
	}
	const headers: Record<string, string> = { 'content-type': 'application/json' };
	if (session) headers['authorization'] = `Bearer ${session.accessToken}`;
	try {
		const upstream = await fetchFn(`${CATALOG_API}/v1/access/tuples`, {
			method,
			headers,
			body: JSON.stringify({ user, relation, object }),
		});
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi access-tuples proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};

// GET lists/filters the estate's tuples — a plain bearer-forwarded read, gated BY THE CATALOG's own
// estate-admin bar (the /v1/events gate). Contributed by the admin zone's FGA workbench.
export const GET = makeCatalogProxy(env);

export const POST: RequestHandler = async ({ request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to write access tuples' }, { status: 401 });
	}
	return forward(fetch, locals.session, 'POST', await request.json().catch(() => null));
};

export const DELETE: RequestHandler = async ({ request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to delete access tuples' }, { status: 401 });
	}
	return forward(fetch, locals.session, 'DELETE', await request.json().catch(() => null));
};
