import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Table preview (goal cond 5) — a narrow session-only route beside the GET-only /capi catch-all,
// reusing the catalog's POST /v1/table/{id}/query (Arrow-IPC FILE response). Confused-deputy stance:
// this is an ENUMERATED read — the browser sends only {"limit": N}; the BFF builds the catalog query
// body itself ({k, vector: {}} = a plain scan, no vector/filter/column selection ever forwarded), so
// a hostile client cannot smuggle query shapes through the session-bearing proxy. Reader-gated
// (can_read_data) at the catalog; only the signed-in user's bearer is attached.
const MAX_PREVIEW_ROWS = 500;
const DEFAULT_PREVIEW_ROWS = 50;

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to preview rows' }, { status: 401 });
	}
	let limit = DEFAULT_PREVIEW_ROWS;
	try {
		const body = (await request.json()) as { limit?: unknown };
		if (typeof body.limit === 'number' && Number.isFinite(body.limit)) {
			limit = Math.floor(body.limit);
		}
	} catch {
		// no/invalid body → the default preview size
	}
	limit = Math.min(Math.max(limit, 1), MAX_PREVIEW_ROWS);
	const headers: Record<string, string> = { 'content-type': 'application/json' };
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/query`;
	try {
		// The wire's QueryTableRequest: k = row budget, an EMPTY vector = plain (non-vector) scan.
		const upstream = await fetch(target, {
			method: 'POST',
			headers,
			body: JSON.stringify({ k: limit, vector: {} }),
		});
		return new Response(upstream.body, {
			status: upstream.status,
			headers: {
				'content-type': upstream.headers.get('content-type') ?? 'application/vnd.apache.arrow.file',
			},
		});
	} catch (err) {
		console.error(`capi query proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
