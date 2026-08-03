import { json } from '@sveltejs/kit';
import { readAuditTrail } from '$lib/server/audit-core';
import type { RequestHandler } from './$types';

// THIN shim over `$lib/server/audit-core.ts`, kept alive for exactly one consumer: the workbench
// zone's <rask-lakehouse-audit> element fetches `/lakehouse/api/audit` directly — a custom-element
// bundle cannot import a .remote.ts (the cross-zone blocker open_transport.md records). The zone's
// OWN UI reads through `admin/remote/audit.remote.ts`; both doors run the same core, so gate, SQL,
// flattening and filters cannot drift between them. Delete this the day the elements gain a
// sanctioned way to call remote endpoints.

export const GET: RequestHandler = async ({ url, fetch, locals }) => {
	const result = await readAuditTrail(fetch, locals, {
		since: url.searchParams.get('since') ?? undefined,
		action: url.searchParams.get('action') ?? undefined,
		outcome: url.searchParams.get('outcome') ?? undefined,
		subject: url.searchParams.get('subject') ?? undefined,
		resource: url.searchParams.get('resource') ?? undefined,
	});
	if (!result.ok) return json({ detail: result.detail }, { status: result.status });
	return json({ events: result.events });
};
