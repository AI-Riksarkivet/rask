// #lib/api/lineage — the typed browser client for the lineage plane (`/api/lineage` through the Gateway).
//
// It covers what the console reads from lineage: the event feed and the DLQ admin pair. One client,
// parameterised by a transport that carries the base path and the person's bearer.
//
// Two fetch postures, deliberately: `getJSON` swallows errors to `null` for reads where an empty state
// and an outage read the same to the user, while `requestJSON` keeps the status for anything where
// 401 ("sign in") ≠ 403 (rung denial) ≠ 0 (offline) must render differently.
import type { ApiResult } from '../client';
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
	/** Replay one staged event, as the signed-in person. Writer-gated at the lineage service
	 *  (can_write_data on outputs). */
	replayDlq: (runId: string) => Promise<ApiResult<DlqReplayResponse>>;
}

/** The two JSON verbs the client needs, already bound to the lineage base: paths are lineage's own. */
export interface LineageTransport {
	getJSON: <T>(path: string) => Promise<T | null>;
	requestJSON: <T = unknown>(path: string, init?: RequestInit) => Promise<ApiResult<T>>;
}

/** Bind the lineage client to a transport, so every path, query string and encoding below stays
 *  single-sourced. */
export function createLineageClient({ getJSON, requestJSON }: LineageTransport): LineageClient {
	return {
		fetchEvents: (opts = {}) => {
			const p = new URLSearchParams();
			if (opts.after) p.set('after', String(opts.after));
			if (opts.limit) p.set('limit', String(opts.limit));
			if (opts.summary) p.set('summary', 'true');
			const qs = p.toString();
			return getJSON<Events>(`events${qs ? `?${qs}` : ''}`);
		},
		fetchDlq: (limit = 100) => requestJSON<DlqBacklog>(`admin/dlq?limit=${limit}`),
		replayDlq: (runId) =>
			requestJSON<DlqReplayResponse>(`admin/dlq/${enc(runId)}/replay`, { method: 'POST' }),
	};
}
