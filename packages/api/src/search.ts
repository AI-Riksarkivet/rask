// @rask/api/search — line FTS + catalog search/browse.

export interface SearchHit {
	batch_id: string;
	page_id: string;
	page_idx: number;
	line_id: string;
	line_idx: number;
	text: string;
	confidence: number;
	hpos: number;
	vpos: number;
	width: number;
	height: number;
	polygon: string;
	thumb_key: string;
	thumb_url: string | null;
	/** Parent volume's EAD row, or null when the batch isn't in the harvested
	 *  catalog. Backend joins by batch_id == bild_id at search time. The
	 *  embedded row doesn't carry the listed/cached/transcribed flags — we
	 *  already know it's transcribed (otherwise it wouldn't be in the line
	 *  index in the first place). */
	catalog: CatalogHit | null;
}

export interface SearchResponse {
	ok: boolean;
	query: string;
	count: number;
	hits: SearchHit[];
}

export interface SearchStats {
	available: boolean;
	rows: number;
}

export async function searchLines(query: string, limit = 50): Promise<SearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/search?${params}`);
	if (!res.ok) throw new Error(`searchLines: HTTP ${res.status}`);
	return res.json();
}

export async function searchStats(): Promise<SearchStats> {
	const res = await fetch('/api/search/stats');
	if (!res.ok) throw new Error(`searchStats: HTTP ${res.status}`);
	return res.json();
}

export interface CatalogHit {
	id: string;
	reference_code: string;
	archive_code: string;
	fonds_id: string;
	fonds_title: string;
	series_id: string;
	series_title: string;
	volume_id: string;
	volume_title: string;
	date_text: string;
	date_start: number | null;
	date_end: number | null;
	description: string;
	bild_id: string;
	bildvisning_url: string;
	iiif_manifest: string;
	thumbnail_url: string;
	/** Three nested local-state flags (transcribed ⊂ cached ⊂ listed):
	 *  - listed:      bild_id is in batches.db (we know the manifest).
	 *  - cached:      cached_pages > 0 (at least one image downloaded).
	 *  - transcribed: transcribed_pages > 0 (at least one ALTO produced).
	 *  Used to pick the click target (our viewer when cached, Riksarkivet
	 *  otherwise) and to render escalating progress badges. */
	listed: boolean;
	cached: boolean;
	transcribed: boolean;
}

export interface CatalogSearchResponse {
	ok: boolean;
	query: string;
	count: number;
	hits: CatalogHit[];
}

export async function searchCatalog(query: string, limit = 50): Promise<CatalogSearchResponse> {
	const params = new URLSearchParams({ q: query, limit: String(limit) });
	const res = await fetch(`/api/catalog/search?${params}`);
	if (!res.ok) throw new Error(`searchCatalog: HTTP ${res.status}`);
	return res.json();
}

export async function catalogStats(): Promise<SearchStats> {
	const res = await fetch('/api/catalog/search/stats');
	if (!res.ok) throw new Error(`catalogStats: HTTP ${res.status}`);
	return res.json();
}

export type CatalogTier = 'listed' | 'cached' | 'transcribed';

export interface CatalogBrowseResponse {
	ok: boolean;
	tier: CatalogTier;
	count: number;
	total: number;
	offset: number;
	hits: CatalogHit[];
}

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
	return res.json();
}

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
	const row = await res.json();
	return { listed: false, cached: false, transcribed: false, ...row };
}
