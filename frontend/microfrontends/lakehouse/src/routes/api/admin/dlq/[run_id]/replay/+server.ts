import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

// #83 DLQ replay — a narrow session-only write beside the GET-only /api lineage proxy. The read-only proxy
// deliberately does NOT export POST (attaching the service credential to a write would let an anonymous
// visitor forge writes into the governed graph — the 2026-07-13 confused-deputy hole). This route forwards
// ONLY the signed-in user's bearer, NEVER the service token, so the lineage service's writer gate
// (can_write_data on the parked event's outputs) is enforced against a real user; auth-on + no session → 401.
export const POST: RequestHandler = async ({ params, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to replay a lineage event' }, { status: 401 });
	}
	const headers: Record<string, string> = {};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${LINEAGE_API}/admin/dlq/${encodeURIComponent(params.run_id)}/replay`;
	try {
		const res = await fetch(target, { method: 'POST', headers });
		return new Response(res.body, {
			status: res.status,
			headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`api dlq replay proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
