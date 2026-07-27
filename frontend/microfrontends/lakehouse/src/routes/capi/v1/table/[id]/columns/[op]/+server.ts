import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// Column ops (#74 schema evolution) — narrow session-only routes beside the GET-only /capi catch-all, with
// an explicit allowlist (add|alter|drop → the catalog's {op}_columns; backfill → the async
// backfill_column job; field-meta|table-meta → the per-field and table-level metadata routes for the
// #74 tail). Forwards ONLY the signed-in user's bearer, so the
// catalog's writer-tier gate (can_write_data) is enforced against a real user; an anonymous visitor on an
// OIDC tier is refused here. The upstream may be a two-segment path (schema_metadata/update) — it composes
// straight onto /v1/table/{id}/ the same way.
const OPS: Record<string, string> = {
	add: 'add_columns',
	alter: 'alter_columns',
	drop: 'drop_columns',
	backfill: 'backfill_column',
	'field-meta': 'update_field_metadata',
	'table-meta': 'schema_metadata/update',
};

export const POST: RequestHandler = async ({ params, request, fetch, locals }) => {
	// Object.hasOwn, not a bare `OPS[params.op]` truthiness check: a prototype key like `op="toString"`
	// would otherwise resolve to a Function (truthy) and slip past the 404 guard. Not exploitable (the
	// result is never placed in the URL — only the allowlisted `upstream` is — so no traversal), but the
	// allowlist must reject anything outside its OWN keys. (audit 2026-07-20)
	if (!Object.hasOwn(OPS, params.op)) {
		return json({ detail: 'not found' }, { status: 404 });
	}
	const upstream = OPS[params.op];
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to change the schema' }, { status: 401 });
	}
	const headers: Record<string, string> = {
		'content-type': request.headers.get('content-type') ?? 'application/json',
	};
	if (locals.session) {
		headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	}
	const target = `${CATALOG_API}/v1/table/${encodeURIComponent(params.id)}/${upstream}`;
	try {
		const res = await fetch(target, { method: 'POST', headers, body: await request.text() });
		return new Response(res.body, {
			status: res.status,
			headers: { 'content-type': res.headers.get('content-type') ?? 'application/json' },
		});
	} catch (err) {
		console.error(`capi columns ${params.op} proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
