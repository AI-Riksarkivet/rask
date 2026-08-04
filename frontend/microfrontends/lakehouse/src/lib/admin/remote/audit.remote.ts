import { getRequestEvent, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { readAuditTrail, type AuditEvent } from '$lib/server/audit-core';

// The zone UI's door to the audit trail (the transport ruling blocked-port #1 — the last one). The
// implementation lives in `$lib/server/audit-core.ts` (once shared with an `/api/audit` route that
// stays alive for the workbench zone's <rask-lakehouse-audit> element; this file is only the
// remote-function binding.

export const fetchAuditTrail = query(
	v.object({
		since: v.optional(v.string()),
		action: v.optional(v.string()),
		outcome: v.optional(v.string()),
		subject: v.optional(v.string()),
		resource: v.optional(v.string()),
	}),
	async (params): Promise<ApiResult<{ events: AuditEvent[] }>> => {
		const { fetch, locals } = getRequestEvent();
		const result = await readAuditTrail(fetch, locals, params);
		return result.ok
			? { ok: true, data: { events: result.events } }
			: { ok: false, status: result.status, detail: result.detail };
	},
);
