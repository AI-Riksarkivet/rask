import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { query } from '$lib/remote.svelte';

// The audit trail's read. The trail is the `lance.audit` log every lakehouse service emits over
// OpenTelemetry, stored and queried by the platform's telemetry backend; no lakehouse door serves it
// and the console has no backend of its own to query that store, so this answers 501 with where the
// trail lives. The page and its filters stay, kept for now by the owner's decision (XC-192).

export type AuditEvent = {
	timestamp: string;
	action: string;
	outcome: string;
	subject: string;
	resource: string;
};

export const fetchAuditTrail = query(
	v.object({
		since: v.optional(v.string()),
		action: v.optional(v.string()),
		outcome: v.optional(v.string()),
		subject: v.optional(v.string()),
		resource: v.optional(v.string()),
	}),
	async (): Promise<ApiResult<{ events: AuditEvent[] }>> => ({
		ok: false,
		status: 501,
		detail:
			"the audit trail is the platform telemetry store's (the lance.audit log); this console has no backend to query it",
	}),
);
