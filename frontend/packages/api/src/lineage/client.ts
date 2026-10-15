// @rask/api/lineage — the typed browser client for the lineage plane, served through a zone's own
// same-origin `/api/*` BFF proxy.
//
// It covers what the lineage service serves to a zone: the event feed and the DLQ admin pair. One
// client, parameterised by the zone's `BffClient` (which carries the base path).
//
// Two fetch postures, deliberately: `getJSON` swallows errors to `null` for reads where an empty state
// and an outage read the same to the user, while `requestJSON` keeps the status for anything where
// 401 ("sign in") ≠ 403 (rung denial) ≠ 0 (offline) must render differently.
import type { ApiResult, BffClient } from '../client';
import type { DlqBacklog, DlqReplayResponse, Events } from './types';

const enc = encodeURIComponent;

export interface LineageClient {
	fetchEvents: (opts?: {
		after?: number;
		limit?: number;
		summary?: boolean;
	}) => Promise<Events | null>;
	/** #83 DLQ ops — the transactional-outbox backlog (admin). Status-aware: a 401 on a governed stack
	 *  must read differently from an empty backlog. */
	fetchDlq: (limit?: number) => Promise<ApiResult<DlqBacklog>>;
	/** Replay one staged event — a session-only write (its own /api/admin/dlq/{run}/replay BFF route
	 *  forwards the user's bearer, never the service token). Writer-gated at the lineage service
	 *  (can_write_data on outputs). */
	replayDlq: (runId: string) => Promise<ApiResult<DlqReplayResponse>>;
}

/** Bind the lineage client to a zone's `BffClient` (see `src/lib/http.ts` in the zone).
 *
 *  Only the two JSON verbs are taken, so the SAME client can also be bound SERVER-side — a zone's
 *  `.remote.ts` supplies a `getJSON`/`requestJSON` pair that reaches the lineage service directly with
 *  the request's own credential, and every path, query-string and encoding below stays single-sourced
 *  instead of being transcribed into the remote functions. */
export function createLineageClient({
	getJSON,
	requestJSON,
}: Pick<BffClient, 'getJSON' | 'requestJSON'>): LineageClient {
	return {
		fetchEvents: (opts = {}) => {
			const p = new URLSearchParams();
			if (opts.after) p.set('after', String(opts.after));
			if (opts.limit) p.set('limit', String(opts.limit));
			if (opts.summary) p.set('summary', 'true');
			const qs = p.toString();
			return getJSON<Events>(`events${qs ? `?${qs}` : ''}`);
		},
		fetchDlq: (limit = 100) => requestJSON<DlqBacklog>('/api', `admin/dlq?limit=${limit}`),
		replayDlq: (runId) =>
			requestJSON<DlqReplayResponse>('/api', `admin/dlq/${enc(runId)}/replay`, { method: 'POST' }),
	};
}
