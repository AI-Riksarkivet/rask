import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Data-plane CREATE-with-data — the FIRST write of a table, which the zone had no door for.
//
// `POST /declare` reserves an id and writes no bytes, so the reserved table has no dataset at its
// location. The append door beside this one (`/insert`) opens that dataset to coerce the incoming
// batch to its schema, so it answers 404 for exactly the table a user just declared: declare →
// insert was a dead end, and the registry's "Declare table" form was the only way into it.
//
// The catalog was never the blocker. `dataplane._create_table_direct` handles a declared-only table
// explicitly — it "has no readable dataset, so every mode simply lands the first data version into
// its already-declared location" — which is what makes the multi-step create idempotent and
// crash-safe. Only this proxy was missing, so the browser could not reach it.
//
// Mirrors the `insert` and `merge_insert` proxies exactly — session-only, binary body streamed
// through, query forwarded verbatim so `?mode=` survives — because three proxies to one catalog that
// differ in their auth stance is how one of them ends up wrong.
export const POST: RequestHandler = async ({ params, request, url, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to create a table' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/vnd.apache.arrow.stream',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/create${url.search}`;
	try {
		const body = await request.arrayBuffer();
		const upstream = await fetch(target, { method: 'POST', headers, body });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi create proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
