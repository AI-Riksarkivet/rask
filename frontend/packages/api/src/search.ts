// @rask/api/search — line FTS + EAD catalog search.
// P7a: the batches.db-coupled surface (browse tiers, /batches/{id}/catalog, the
// listed/cached/transcribed enrichment flags) died with the batches table; hits are
// pure EAD rows now. The whole module retires with the R6/R20 media wave.

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

export async function searchLines(
	query: string,
	limit = 50,
	fetchFn: typeof fetch = fetch,
): Promise<SearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetchFn(`/api/search?${params}`);
	if (!res.ok) throw new Error(`searchLines: HTTP ${res.status}`);
	return parse(SearchResponseSchema, await res.json());
}

export async function searchStats(fetchFn: typeof fetch = fetch): Promise<SearchStats> {
	const res = await fetchFn('/api/search/stats');
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

export async function searchCatalog(
	query: string,
	limit = 50,
	fetchFn: typeof fetch = fetch,
): Promise<CatalogSearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetchFn(`/api/catalog/search?${params}`);
	if (!res.ok) throw new Error(`searchCatalog: HTTP ${res.status}`);
	return parse(CatalogSearchResponseSchema, await res.json());
}

export async function catalogStats(fetchFn: typeof fetch = fetch): Promise<SearchStats> {
	const res = await fetchFn('/api/catalog/search/stats');
	if (!res.ok) throw new Error(`catalogStats: HTTP ${res.status}`);
	return parse(SearchStatsSchema, await res.json());
}
