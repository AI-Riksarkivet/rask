import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	createLineageClient,
	type DatasetGovernance,
	type DlqBacklog,
	type DlqReplayResponse,
} from '@rask/api/lineage';
import { lineageAuthHeaders } from '@rask/api/runs-feed';

// The lineage plane's WRITE surface plus the two reads that belong to it, in the zone's
// remote-function dialect (open_transport.md, area 2) — same names, same `ApiResult` shapes as the
// `$lib/api` client they replace, transport only.
//
// It wraps the SHARED `createLineageClient` rather than re-typing its paths: the client only ever
// needed a `getJSON`/`requestJSON` pair, so the pair below reaches the lineage service DIRECTLY from
// the zone server (no same-origin hop through `/api/*`), and every path, query string and
// `encodeURIComponent` stays single-sourced in `@rask/api/lineage`.
//
// THE CREDENTIAL POSTURE IS THE POINT, and it is asymmetric on purpose (packages/api/src/bff.ts:190):
// a READ may fall back to the service credential so a governed stack serves reads without a per-user
// login; a WRITE never may — attaching the service token to a write would let an anonymous visitor
// forge writes into the governed graph (the 2026-07-13 confused-deputy hole). The deleted routes made
// that door twice (a GET-only proxy plus three session-only write routes); here it is one predicate.
//
// No single-flight `refresh()` on these commands, deliberately: every write ECHOES the whole updated
// record (the governance quad) or a one-shot verdict (replay), and the panels render from that echo.
// Refreshing the sibling read would be a second upstream call for a value the write just returned.

const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

/** A read is a liveness-bounded call, not a page load — the same ceiling the browser client used. */
const TIMEOUT_MS = 8000;

/** This request's lineage credential. Reads fall back to the service identity when there is no
 *  session; writes carry the signed-in user's bearer or nothing at all. */
function authHeaders(isRead: boolean): Record<string, string> {
	const accessToken = getRequestEvent().locals.session?.accessToken;
	if (!isRead) return accessToken ? { authorization: `Bearer ${accessToken}` } : {};
	return lineageAuthHeaders({
		accessToken,
		serviceToken: env.LINEAGE_SERVICE_TOKEN,
		serviceId: env.LINEAGE_SERVICE_ID,
	});
}

/** The write-route short-circuit the deleted `+server.ts` files made: on an OIDC-configured tier a
 *  write must be attributable to a real user, and it is refused before the request leaves the zone. */
function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

/** The lineage client, bound to THIS request: same surface as the browser binding, server transport. */
function lineage() {
	const { fetch } = getRequestEvent();

	const requestJSON = async <T>(
		_base: string,
		path: string,
		init?: RequestInit,
	): Promise<ApiResult<T>> => {
		const method = init?.method ?? 'GET';
		const isRead = method === 'GET' || method === 'HEAD';
		try {
			const res = await fetch(`${LINEAGE_API}/${path}`, {
				...init,
				headers: {
					...authHeaders(isRead),
					...(init?.body ? { 'content-type': 'application/json' } : {}),
				},
				signal: AbortSignal.timeout(TIMEOUT_MS),
			});
			const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
			if (!res.ok) {
				const detail = typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`;
				return { ok: false, status: res.status, detail };
			}
			return { ok: true, data: body as T };
		} catch (err) {
			// What the deleted routes answered when the upstream was unreachable — kept verbatim so a
			// panel's offline branch reads the same sentence it always did.
			console.error(`lineage remote upstream failure: ${String(err)}`);
			return { ok: false, status: 502, detail: 'lineage upstream unavailable' };
		}
	};

	const getJSON = async <T>(path: string): Promise<T | null> => {
		const res = await requestJSON<T>('/api', path);
		return res.ok ? res.data : null;
	};

	return createLineageClient({ getJSON, requestJSON });
}

/** #83 DLQ ops — the transactional-outbox backlog. Governed per-dataset by the service, so the
 *  status split (401 sign-in vs an empty backlog) is what the panel renders on. */
export const fetchDlq = query(
	v.optional(v.number(), 100),
	async (limit): Promise<ApiResult<DlqBacklog>> => lineage().fetchDlq(limit),
);

/** Replay one staged event (re-ingest + drop) — a session-only write: writer-gated at the lineage
 *  service (`can_write_data` on the parked event's outputs) against a REAL user, never the service
 *  identity. Auth-on + no session → 401 without the request leaving the zone. */
export const replayDlq = command(
	v.string(),
	async (runId): Promise<ApiResult<DlqReplayResponse>> => {
		if (signedOut()) {
			return { ok: false, status: 401, detail: 'sign in to replay a lineage event' };
		}
		return lineage().replayDlq(runId);
	},
);

/** The dataset's governance record (#49): curated tags + description with their last-writer
 *  attribution. A swallowed failure is `null`, exactly as before — the panel says "unavailable"
 *  rather than showing an empty record as if the metadata were absent. */
export const fetchGovernance = query(
	v.string(),
	async (name): Promise<DatasetGovernance | null> => lineage().fetchGovernance(name),
);

const TagWrite = v.object({ name: v.string(), tag: v.string() });

/** Add one curated tag — session-only, writer-gated, echoing the whole updated record. */
export const addDatasetTag = command(
	TagWrite,
	async ({ name, tag }): Promise<ApiResult<DatasetGovernance>> => {
		if (signedOut()) {
			return { ok: false, status: 401, detail: 'sign in to edit governance tags' };
		}
		return lineage().addDatasetTag(name, tag);
	},
);

/** Remove one curated tag — same door, same echo. */
export const removeDatasetTag = command(
	TagWrite,
	async ({ name, tag }): Promise<ApiResult<DatasetGovernance>> => {
		if (signedOut()) {
			return { ok: false, status: 401, detail: 'sign in to edit governance tags' };
		}
		return lineage().removeDatasetTag(name, tag);
	},
);

/** Set the dataset description — the same session-only write, with its own refusal sentence. */
export const setDatasetDescription = command(
	v.object({ name: v.string(), description: v.string() }),
	async ({ name, description }): Promise<ApiResult<DatasetGovernance>> => {
		if (signedOut()) {
			return { ok: false, status: 401, detail: 'sign in to edit the description' };
		}
		return lineage().setDatasetDescription(name, description);
	},
);
