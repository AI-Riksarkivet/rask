import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { Store, StoreDraft } from '../storage';

// The stores registry: every object store with the tier it backs, and the attach write. The catalog
// serves no such registry. A store is a warehouse's bucket (`/management/v1/warehouses` carries the
// bucket and endpoint, on the warehouses page), and a tier is a namespace inside a warehouse, so
// "which store backs which tier" is a fact no door states. The page and its form stay, kept by the
// owner's decision (XC-192); both answer that, without a request.

const NO_REGISTRY: { ok: false; status: number; detail: string } = {
	ok: false,
	status: 501,
	detail:
		'the catalog serves no stores-by-tier registry: a store is a warehouse bucket (see Warehouses), and no door maps stores to tiers',
};

const StorageRoleSchema = v.picklist([
	'raw',
	'bronze',
	'silver',
	'gold',
	'derived',
	'observability',
]);

/** The tier → store view. */
export const listStoresByTier = query(
	async (): Promise<ApiResult<Record<string, Store[]>>> => NO_REGISTRY,
);

/** Attach a bucket for browsing: refused with the same reason, so the form says why. */
export const attachStore = command(
	v.object({
		name: v.string(),
		bucket: v.string(),
		role: StorageRoleSchema,
		endpoint: v.nullable(v.string()),
		description: v.string(),
	}),
	async (_draft: StoreDraft): Promise<ApiResult<{ stores: Store[] }>> => NO_REGISTRY,
);
