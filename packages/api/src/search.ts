// @rask/api/search — line FTS + catalog search/browse.

import * as v from 'valibot';
import { parse } from './parse.js';

export const CatalogHitSchema = v.object({
	id: v.string(),
	reference_code: v.string(),
	archive_code: v.string(),
	fonds_id: v.string(),
	fonds_title: v.string(),
	series_id: v.string(),
	series_title: v.string(),
	volume_id: v.string(),
	volume_title: v.string(),
	date_text: v.string(),
	date_start: v.nullable(v.number()),
	date_end: v.nullable(v.number()),
	description: v.string(),
	bild_id: v.string(),
	bildvisning_url: v.string(),
	iiif_manifest: v.string(),
	thumbnail_url: v.string(),
	/** Three nested local-state flags (transcribed ⊂ cached ⊂ listed):
	 *  - listed:      bild_id is in batches.db (we know the manifest).
	 *  - cached:      cached_pages > 0 (at least one image downloaded).
	 *  - transcribed: transcribed_pages > 0 (at least one ALTO produced).
	 *  Used to pick the click target (our viewer when cached, Riksarkivet
	 *  otherwise) and to render escalating progress badges. */
	listed: v.boolean(),
	cached: v.boolean(),
	transcribed: v.boolean(),
});
export type CatalogHit = v.InferOutput<typeof CatalogHitSchema>;

export const SearchHitSchema = v.object({
	batch_id: v.string(),
	page_id: v.string(),
	page_idx: v.number(),
	line_id: v.string(),
	line_idx: v.number(),
	text: v.string(),
	confidence: v.number(),
	hpos: v.number(),
	vpos: v.number(),
	width: v.number(),
	height: v.number(),
	polygon: v.string(),
	thumb_key: v.string(),
	thumb_url: v.nullable(v.string()),
	/** Parent volume's EAD row, or null when the batch isn't in the harvested
	 *  catalog. Backend joins by batch_id == bild_id at search time. The
	 *  embedded row doesn't carry the listed/cached/transcribed flags — we
	 *  already know it's transcribed (otherwise it wouldn't be in the line
	 *  index in the first place). */
	catalog: v.nullable(CatalogHitSchema),
});
export type SearchHit = v.InferOutput<typeof SearchHitSchema>;

export const SearchResponseSchema = v.object({
	ok: v.boolean(),
	query: v.string(),
	count: v.number(),
	hits: v.array(SearchHitSchema),
});
export type SearchResponse = v.InferOutput<typeof SearchResponseSchema>;

export const SearchStatsSchema = v.object({
	available: v.boolean(),
	rows: v.number(),
});
export type SearchStats = v.InferOutput<typeof SearchStatsSchema>;

export async function searchLines(query: string, limit = 50): Promise<SearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/search?${params}`);
	if (!res.ok) throw new Error(`searchLines: HTTP ${res.status}`);
	return parse(SearchResponseSchema, await res.json());
}

export async function searchStats(): Promise<SearchStats> {
	const res = await fetch('/api/search/stats');
	if (!res.ok) throw new Error(`searchStats: HTTP ${res.status}`);
	return parse(SearchStatsSchema, await res.json());
}

export const CatalogSearchResponseSchema = v.object({
	ok: v.boolean(),
	query: v.string(),
	count: v.number(),
	hits: v.array(CatalogHitSchema),
});
export type CatalogSearchResponse = v.InferOutput<typeof CatalogSearchResponseSchema>;

export async function searchCatalog(query: string, limit = 50): Promise<CatalogSearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/catalog/search?${params}`);
	if (!res.ok) throw new Error(`searchCatalog: HTTP ${res.status}`);
	return parse(CatalogSearchResponseSchema, await res.json());
}

export async function catalogStats(): Promise<SearchStats> {
	const res = await fetch('/api/catalog/search/stats');
	if (!res.ok) throw new Error(`catalogStats: HTTP ${res.status}`);
	return parse(SearchStatsSchema, await res.json());
}

export type CatalogTier = 'listed' | 'cached' | 'transcribed';

export const CatalogBrowseResponseSchema = v.object({
	ok: v.boolean(),
	tier: v.picklist(['listed', 'cached', 'transcribed']),
	count: v.number(),
	total: v.number(),
	offset: v.number(),
	hits: v.array(CatalogHitSchema),
});
export type CatalogBrowseResponse = v.InferOutput<typeof CatalogBrowseResponseSchema>;

/** Browse mode — list batches at >= `tier` from batches.db, enriched with
 *  EAD catalog metadata. Returns at most `limit` rows (server-capped at 2000)
 *  starting at `offset`. Used by the /browse page. */
export async function browseCatalog(
	tier: CatalogTier = 'cached',
	limit = 500,
	offset = 0,
): Promise<CatalogBrowseResponse> {
	const params = new URLSearchParams({ tier, limit: String(limit), offset: String(offset) });
	const res = await fetch(`/api/catalog/browse?${params}`);
	if (!res.ok) throw new Error(`browseCatalog: HTTP ${res.status}`);
	return parse(CatalogBrowseResponseSchema, await res.json());
}

// The /batches/{id}/catalog endpoint returns just the EAD row, not the
// {listed, cached, transcribed} enrichment flags (those are search-time only).
// Parse the partial row leniently, then fill the flags as false below.
const CatalogRowSchema = v.object({
	id: v.string(),
	reference_code: v.string(),
	archive_code: v.string(),
	fonds_id: v.string(),
	fonds_title: v.string(),
	series_id: v.string(),
	series_title: v.string(),
	volume_id: v.string(),
	volume_title: v.string(),
	date_text: v.string(),
	date_start: v.nullable(v.number()),
	date_end: v.nullable(v.number()),
	description: v.string(),
	bild_id: v.string(),
	bildvisning_url: v.string(),
	iiif_manifest: v.string(),
	thumbnail_url: v.string(),
});

/** Direct catalog lookup by batch_id (== bild_id). Returns null when the
 *  batch isn't in the harvested EAD catalog (e.g. test batches), so the
 *  viewer can render conditionally without a separate error path. */
export async function getBatchCatalog(batchId: string): Promise<CatalogHit | null> {
	const res = await fetch(`/api/batches/${encodeURIComponent(batchId)}/catalog`);
	if (res.status === 404) return null;
	if (!res.ok) throw new Error(`getBatchCatalog: HTTP ${res.status}`);
	// The endpoint returns just the row, not the {listed, cached, transcribed}
	// flags — those are search-time enrichment. Fill them in as false so the
	// type matches; consumers reading them will see "no local state known".
	const row = parse(CatalogRowSchema, await res.json());
	return { listed: false, cached: false, transcribed: false, ...row };
}
