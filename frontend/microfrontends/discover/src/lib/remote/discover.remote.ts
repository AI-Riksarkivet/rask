import * as v from 'valibot';
import { query, getRequestEvent } from '$app/server';
import {
	browseCatalog,
	searchStats,
	catalogStats,
	searchLines,
	searchCatalog,
	listPages,
	getBatchCatalog,
	type CatalogBrowseResponse,
	type SearchStats,
	type SearchResponse,
	type CatalogSearchResponse,
	type PageEntry,
	type CatalogHit,
} from '@rask/api';

// Discover microfrontend data layer — SvelteKit remote functions (server-only).
//
// THE ONE PATTERN: every read is a `query()` whose body calls a `@rask/api`
// function, passing `getRequestEvent().fetch`. That `event.fetch` is SvelteKit's
// request-scoped fetch (same one `load` receives): it resolves the relative
// `/api/*` URLs `@rask/api` uses against the request origin during SSR (a bare
// global `fetch` has no origin on the server), inherits cookies, and inlines the
// response into the SSR payload so hydration doesn't refetch. So the query()
// SSR-renders the first frame (no onMount/$effect waterfall) and REUSES
// @rask/api's schemas + parse — zero duplicated fetch/validation per app.
//
// No-arg queries (cache key is the function identity); param queries take a
// valibot schema (the cache key is the deep-sorted args). All read-only.
//
// Reads that legitimately stay OUTSIDE the query pattern (called directly):
//   - fetchAlto (returns raw XML text for the canvas, not JSON for markup)
//   - imageUrl/altoUrl (plain hrefs, not fetches)

/** Browse mode — list catalog volumes at >= `tier`, enriched with EAD metadata.
 *  Param query: re-keys on `tier`/`limit`/`offset` so navigation re-runs it. */
const BrowseArgs = v.object({
	tier: v.picklist(['listed', 'cached', 'transcribed']),
	limit: v.optional(v.number(), 2000),
	offset: v.optional(v.number(), 0),
});

export const browseCatalogTier = query(
	BrowseArgs,
	async ({ tier, limit, offset }): Promise<CatalogBrowseResponse> => {
		return browseCatalog(tier, limit, offset, getRequestEvent().fetch);
	},
);

/** Lines FTS index stats. SSR-rendered initial frame. */
export const getSearchStats = query(async (): Promise<SearchStats> => {
	return searchStats(getRequestEvent().fetch);
});

/** Catalog FTS index stats. SSR-rendered initial frame. */
export const getCatalogStats = query(async (): Promise<SearchStats> => {
	return catalogStats(getRequestEvent().fetch);
});

/** Lines FTS search. User-triggered (run via `.run()` in the submit handler),
 *  keyed on `{ q, limit }`. */
const SearchArgs = v.object({
	q: v.string(),
	limit: v.optional(v.number(), 50),
});

export const searchLinesQ = query(SearchArgs, async ({ q, limit }): Promise<SearchResponse> => {
	return searchLines(q, limit, getRequestEvent().fetch);
});

/** Catalog FTS search. User-triggered (run via `.run()` in the submit handler),
 *  keyed on `{ q, limit }`. */
export const searchCatalogQ = query(
	SearchArgs,
	async ({ q, limit }): Promise<CatalogSearchResponse> => {
		return searchCatalog(q, limit, getRequestEvent().fetch);
	},
);

/** Page listing for a volume. Param query: re-keys on `volume`. */
const VolumeArgs = v.object({ volume: v.string() });

export const listVolumePages = query(VolumeArgs, async ({ volume }): Promise<PageEntry[]> => {
	return listPages(volume, getRequestEvent().fetch);
});

/** EAD catalog row for a volume (null when not in the harvested catalog).
 *  Param query: re-keys on `volume`. */
export const getVolumeCatalog = query(
	VolumeArgs,
	async ({ volume }): Promise<CatalogHit | null> => {
		return getBatchCatalog(volume, getRequestEvent().fetch);
	},
);
