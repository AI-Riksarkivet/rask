import { command, query } from '$lib/remote.svelte';
import { LINEAGE_API } from '$lib/upstreams';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { getJSON, requestJSON } from '@rask/api/client';
import { auth } from '$lib/auth.svelte';
import { createLineageClient, type DlqBacklog, type DlqReplayResponse } from '@rask/api/lineage';

// The lineage plane's DLQ surface (its backlog read and its replay write). It wraps the SHARED
// `createLineageClient`, so every path, query string and `encodeURIComponent` stays single-sourced in
// `@rask/api/lineage`. Both carry only the signed-in person's bearer.
//
// No single-flight `refresh()` on the replay command, deliberately: it ECHOES a one-shot verdict and
// the panel renders from that echo.

const lineage = createLineageClient({
	getJSON: (path) => getJSON(LINEAGE_API, path),
	requestJSON: (path, init) => requestJSON(LINEAGE_API, path, init),
});

/** #83 DLQ ops — the transactional-outbox backlog. Governed per-dataset by the service, so the
 *  status split (401 sign-in vs an empty backlog) is what the panel renders on. */
export const fetchDlq = query(
	v.optional(v.number(), 100),
	async (limit): Promise<ApiResult<DlqBacklog>> => lineage.fetchDlq(limit),
);

/** Replay one staged event (re-ingest + drop) — a session-only write: writer-gated at the lineage
 *  service (`can_write_data` on the parked event's outputs) against a REAL user. Auth-on + no
 *  session → 401 without the request leaving the browser. */
export const replayDlq = command(
	v.string(),
	async (runId): Promise<ApiResult<DlqReplayResponse>> => {
		if (auth.enabled && !auth.session) {
			return { ok: false, status: 401, detail: 'sign in to replay a lineage event' };
		}
		return lineage.replayDlq(runId);
	},
);
