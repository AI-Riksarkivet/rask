// @rask/api/ingest — the P7a ingestion door: the medallion producer's POST /ingest-iiif
// (gateway route /api/ingest-iiif → lance-ray). Replaces the retired batches.ts upload/sync
// surface: a volume is harvested from the IIIF Image API into the raw page-image Lance dataset,
// and the cascade (raw→bronze promotion, then the HTR movers) runs event-driven from its ONE
// raw-write lineage event — there is nothing to poll here, the runs feed carries progress.

import * as v from 'valibot';
import { parse } from './parse.js';

export const IngestAcceptedSchema = v.object({
	status: v.literal('ingested'),
	token: v.string(),
	dataset: v.string(),
	pages: v.string(),
});
export type IngestAccepted = v.InferOutput<typeof IngestAcceptedSchema>;

/** Harvest one IIIF volume into the raw page-image dataset (202 on success).
 *  Non-2xx surfaces the problem+json detail so the form can render the real refusal
 *  (409 head-off / 400 ceilings / 503 retryable). */
export async function ingestIIIFVolume(
	volumeId: string,
	options: { maxPages?: number; project?: string; idempotencyKey?: string } = {},
	fetchFn: typeof fetch = fetch,
): Promise<IngestAccepted> {
	const headers: Record<string, string> = { 'content-type': 'application/json' };
	if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
	const res = await fetchFn('/api/ingest-iiif', {
		method: 'POST',
		headers,
		body: JSON.stringify({
			volume_id: volumeId,
			...(options.maxPages !== undefined ? { max_pages: options.maxPages } : {}),
			...(options.project ? { project: options.project } : {}),
		}),
	});
	if (!res.ok) {
		let detail = `HTTP ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) {
				detail = String((body as { detail: unknown }).detail);
			}
		} catch {
			// non-JSON error body — keep the status line
		}
		throw new Error(`ingestIIIFVolume: ${detail}`);
	}
	return parse(IngestAcceptedSchema, await res.json());
}
