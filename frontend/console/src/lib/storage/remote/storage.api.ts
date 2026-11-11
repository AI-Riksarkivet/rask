import { query } from '#lib/reads.svelte.ts';
import type { ApiResult } from '#lib/api/client.ts';
import type { Store } from '../storage';

// The stores registry: every object store with the tier it backs. The catalog
// serves no such registry. A store is a warehouse's bucket (`/management/v1/warehouses` carries the
// bucket and endpoint, on the warehouses page), and a tier is a namespace inside a warehouse, so
// "which store backs which tier" is a fact no door states. The page stays, kept by the owner's
// decision (XC-192), and answers that without a request; it offers no write that could not act.

const NO_REGISTRY: { ok: false; status: number; detail: string } = {
	ok: false,
	status: 501,
	detail:
		'the catalog serves no stores-by-tier registry: a store is a warehouse bucket (see Warehouses), and no door maps stores to tiers',
};

/** The tier → store view. */
export const listStoresByTier = query(
	async (): Promise<ApiResult<Record<string, Store[]>>> => NO_REGISTRY,
);
