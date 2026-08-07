// @rask/api/ingest — the ingest plane's door: POST /api/ingest, GET /api/ingest/{run_id}.
//
// Rewritten 2026-08-03 for the plane that replaces the medallion's IIIF head (open_ingest.md P1).
// Two things changed shape, and both were WRONG here in a way that would have failed at runtime:
//
//   * The response is a run HANDLE, not a result. This module pinned `status: v.literal('ingested')`
//     — a value the new API never sends — so a genuinely async 202 would have thrown at the valibot
//     boundary rather than rendering. That literal could only ever have been written because the old
//     head declared 202 and then blocked through the entire harvest, making "ingested" a plausible
//     word in an ACCEPT response.
//   * The door is SOURCE-AGNOSTIC (I1). A source is a registry entry, so the client takes
//     `{kind, project, dataset, options}` instead of carrying `volume_id` in its signature. Adding
//     S3-prefix ingest must not touch this file — that is the whole point of the registry.
//
// Progress is read from GET /api/ingest/{run_id}: the run's own status is the authority, and a UI
// inferring completion from anything else would disagree with the record it is meant to display.

import * as v from 'valibot';
import { parse } from './parse.js';

/** The 202 body — a handle to a run that has been ACCEPTED, not one that has finished. */
export const IngestAcceptedSchema = v.object({
	run_id: v.string(),
	status: v.string(),
	/** true when an Idempotency-Key resolved to an existing run and no new work was started. */
	deduplicated: v.optional(v.boolean(), false),
});
export type IngestAccepted = v.InferOutput<typeof IngestAcceptedSchema>;

/** Run status. `defect` is set when the run reports success but its lineage record is missing —
 *  A8's "a green sync with no lineage edge is a bug the UI should surface, not report green". */
export const IngestRunSchema = v.object({
	run_id: v.string(),
	status: v.string(),
	units_total: v.number(),
	units_done: v.number(),
	errors: v.record(v.string(), v.string()),
	committed_version: v.nullable(v.number()),
	defect: v.nullable(v.string()),

	// THE PUBLICATION HALF (§D2). The backend has always returned these; this schema did not declare
	// them, so valibot stripped every one at the wire boundary and the run page could not have shown
	// them if it tried. A commit makes rows READABLE; only a publication makes them READY — so a run
	// that committed and did not publish is a distinct state, and dropping these fields is precisely
	// what turns it back into "a green run with a silent hole", which is the thing §D2 exists to stop.
	//
	// `optional` as well as `nullable`: an older ingest build omits the keys entirely rather than
	// sending null, and a hard requirement here would fail the whole parse — turning a partially-known
	// run into an unreadable one, which is worse than a missing field.
	published: v.optional(v.nullable(v.boolean())),
	from_version: v.optional(v.nullable(v.number())),
	to_version: v.optional(v.nullable(v.number())),
	publish_reason: v.optional(v.nullable(v.string())),
	publish_error: v.optional(v.nullable(v.string())),
});
export type IngestRun = v.InferOutput<typeof IngestRunSchema>;

/** How ONE run partitions its writes. Every field optional — omitted means the deployment default.
 *
 *  Per-run rather than per-deployment because the right value belongs to the SOURCE: ten thousand
 *  20 MB page images and a million 40 KB records want opposite fragment shapes, and a rate-limited
 *  IIIF endpoint wants a different fetch concurrency from an S3 bucket. The door resolves these
 *  against its env defaults and REFUSES a `fragment_rows` at or above the queue's unacked ceiling
 *  (2048) with a 400 naming it — that value does not make bigger fragments, it hangs the drain. */
export interface IngestSizing {
	/** Rows accumulated before one Lance fragment is written. Must stay under the ack ceiling. */
	fragment_rows?: number;
	/** …or this many payload bytes, whichever comes first — the limit that fires on media. */
	fragment_bytes?: number;
	/** Units pulled from the queue per round-trip. */
	fetch_batch?: number;
	/** In-flight fetches against the SOURCE — a politeness ceiling for rate-limited endpoints. */
	fetch_concurrency?: number;
}

export interface IngestRequest {
	/** A registered source kind — 's3-prefix' | 'local-dir' (IIIF removed by owner ruling 2026-08-07). The registry is the authority. */
	kind: string;
	project: string;
	dataset: string;
	options?: Record<string, unknown>;
	sizing?: IngestSizing;
	/** Sent as Idempotency-Key. The SAME key resolves to the same run and starts no second workflow. */
	idempotencyKey?: string;
}

async function refuse(res: Response, what: string): Promise<never> {
	// problem+json `detail` carries the real refusal (400 unknown kind, 409, 503 retryable). Falling
	// back to the status line stops a non-JSON error body from masking the failure entirely.
	let detail = `HTTP ${res.status}`;
	try {
		const body: unknown = await res.json();
		if (body && typeof body === 'object' && 'detail' in body) {
			detail = String((body as { detail: unknown }).detail);
		}
	} catch {
		// non-JSON error body — keep the status line
	}
	throw new Error(`${what}: ${detail}`);
}

/** Accept an ingest run. Returns as soon as the run is dispatched — 202 means 202. */
export async function startIngest(
	request: IngestRequest,
	fetchFn: typeof fetch = fetch,
	extraHeaders: Record<string, string> = {},
): Promise<IngestAccepted> {
	// `extraHeaders` exists for the CALLER'S BEARER, and it is not optional in practice on a governed
	// estate. SvelteKit's request-scoped `fetch` forwards cookies but attaches NO `Authorization`
	// header — that is normally the BFF proxy's job — so a server-side call made without this arrives
	// at the door carrying only the gateway's own Dapr app-token, and the door refuses it:
	//
	//     'gateway' is a public front door: its Dapr app-token authenticates the proxy, not the caller
	//
	// which is correct (an app-token proves the proxy, never the human). Measured twice: first from
	// the browser, then again from inside a remote function that assumed request-scoped fetch was
	// enough. The estate's pattern is `locals.session?.accessToken` -> `authorization: Bearer …`
	// (`lakehouse/src/lib/admin/remote/access.remote.ts:47-50`).
	const headers: Record<string, string> = { 'content-type': 'application/json', ...extraHeaders };
	if (request.idempotencyKey) headers['Idempotency-Key'] = request.idempotencyKey;
	const res = await fetchFn('/api/ingest/ingests', {
		method: 'POST',
		headers,
		body: JSON.stringify({
			kind: request.kind,
			project: request.project,
			dataset: request.dataset,
			options: request.options ?? {},
			sizing: request.sizing ?? {},
		}),
	});
	if (!res.ok) return refuse(res, 'startIngest');
	return parse(IngestAcceptedSchema, await res.json());
}

/** Read a run's status. */
export async function getIngestRun(
	runId: string,
	fetchFn: typeof fetch = fetch,
	extraHeaders: Record<string, string> = {},
): Promise<IngestRun> {
	// Same bearer seam as `startIngest` — the READ door is governed too, so a server-side call with
	// no `Authorization` is refused as the gateway rather than served as the user.
	const res = await fetchFn(`/api/ingest/ingests/${encodeURIComponent(runId)}`, { headers: extraHeaders });
	if (!res.ok) return refuse(res, 'getIngestRun');
	return parse(IngestRunSchema, await res.json());
}

/** One field a source kind needs in `options`, described well enough to render. Presentational
 *  minimum on purpose: the adapter validates, and a second copy of its rules here goes stale. */
export const SourceOptionSchema = v.object({
	name: v.string(),
	label: v.string(),
	required: v.optional(v.boolean(), false),
	numeric: v.optional(v.boolean(), false),
	placeholder: v.nullable(v.optional(v.string()), null),
	help: v.nullable(v.optional(v.string()), null),
});
export type SourceOption = v.InferOutput<typeof SourceOptionSchema>;

/** A registered source kind as the door describes it. */
export const SourceDescriptorSchema = v.object({
	kind: v.string(),
	label: v.string(),
	description: v.nullable(v.optional(v.string()), null),
	options: v.array(SourceOptionSchema),
});
export type SourceDescriptor = v.InferOutput<typeof SourceDescriptorSchema>;

/** The source kinds this deployment actually has, and what each one needs.
 *
 *  Replaces `ingestIIIFVolume()`, which hardcoded `kind: 'iiif'` with `project: 'default'` and
 *  `dataset: 'pages'` — the same weld I1 removed from the backend, re-formed one layer out. A
 *  convenience wrapper per kind does not scale to a registry, and worse, it makes the frontend the
 *  place that decides which kinds exist: `S3PrefixSource` was written, tested and unreachable for
 *  months for exactly that reason.
 *
 *  Ask the registry instead. Adding a source then never touches this file, which is gate A9. */
export async function listIngestSources(
	fetchFn: typeof fetch = fetch,
	extraHeaders: Record<string, string> = {},
): Promise<SourceDescriptor[]> {
	const res = await fetchFn('/api/ingest/sources', { headers: extraHeaders });
	if (!res.ok) return refuse(res, 'listIngestSources');
	return parse(v.array(SourceDescriptorSchema), await res.json());
}
