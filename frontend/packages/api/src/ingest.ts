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
});
export type IngestRun = v.InferOutput<typeof IngestRunSchema>;

export interface IngestRequest {
	/** A registered source kind — 'iiif' | 's3-prefix' | 'local-dir'. The registry is the authority. */
	kind: string;
	project: string;
	dataset: string;
	options?: Record<string, unknown>;
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
): Promise<IngestAccepted> {
	const headers: Record<string, string> = { 'content-type': 'application/json' };
	if (request.idempotencyKey) headers['Idempotency-Key'] = request.idempotencyKey;
	const res = await fetchFn('/api/ingest/ingests', {
		method: 'POST',
		headers,
		body: JSON.stringify({
			kind: request.kind,
			project: request.project,
			dataset: request.dataset,
			options: request.options ?? {},
		}),
	});
	if (!res.ok) return refuse(res, 'startIngest');
	return parse(IngestAcceptedSchema, await res.json());
}

/** Read a run's status. */
export async function getIngestRun(
	runId: string,
	fetchFn: typeof fetch = fetch,
): Promise<IngestRun> {
	const res = await fetchFn(`/api/ingest/ingests/${encodeURIComponent(runId)}`);
	if (!res.ok) return refuse(res, 'getIngestRun');
	return parse(IngestRunSchema, await res.json());
}

/** Harvest one IIIF volume — a thin wrapper over {@link startIngest}, kept so the compute zone's
 *  existing form keeps working. IIIF is just a registered kind now; this convenience exists for the
 *  caller's readability, NOT because the door knows anything about IIIF. */
export async function ingestIIIFVolume(
	volumeId: string,
	options: { maxPages?: number; project?: string; idempotencyKey?: string } = {},
	fetchFn: typeof fetch = fetch,
): Promise<IngestAccepted> {
	return startIngest(
		{
			kind: 'iiif',
			project: options.project ?? 'default',
			dataset: 'pages',
			options: {
				volume_id: volumeId,
				...(options.maxPages !== undefined ? { max_pages: options.maxPages } : {}),
			},
			...(options.idempotencyKey ? { idempotencyKey: options.idempotencyKey } : {}),
		},
		fetchFn,
	);
}
