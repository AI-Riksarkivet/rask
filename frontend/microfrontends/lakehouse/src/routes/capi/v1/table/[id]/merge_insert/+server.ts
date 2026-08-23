import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Data-plane MERGE-insert — the write a manual push to bronze is supposed to use.
//
// Not a raw insert, and the difference is the whole point: `when_not_matched_insert_all` is native
// insert-if-not-matched, so re-pushing the same rows CONVERGES rather than landing them twice. A raw
// insert is a blind append, which is why two runs over one source leave 2N rows over N distinct ids.
//
// The catalog door already accepted this; only the zone-side proxy was missing, so the UI had no way
// to reach it. Mirrors the insert proxy beside it exactly — session-only, binary body streamed
// through, query forwarded verbatim so `?on=` and the when_* flags survive — because two proxies to
// one catalog that differ in their auth stance is how one of them ends up wrong.
export const POST: RequestHandler = async ({ params, request, url, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to merge rows' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/vnd.apache.arrow.stream',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/merge_insert${url.search}`;
	try {
		const body = await request.arrayBuffer();
		const upstream = await fetch(target, { method: 'POST', headers, body });
		return new Response(upstream.body, {
			status: upstream.status,
			headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi merge_insert proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
