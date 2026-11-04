import { command, query } from '$lib/remote.svelte';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { Store, StoreDraft } from '../storage';

import { catalogJSON, parsed } from '$lib/doors';
// Transport: the console's catalog door, `$lib/doors`.
// The stores registry, declared with `query`/`command`: reads and the attach write answer the shared
// `ApiResult` union, so the storage page renders every status the catalog gives.

const StoreSchema = v.object({
	name: v.string(),
	bucket: v.string(),
	role: v.picklist(['raw', 'bronze', 'silver', 'gold', 'derived', 'observability']),
	description: v.string(),
	read_only: v.boolean(),
});
const RegistrySchema = v.object({ stores: v.array(StoreSchema) });
const TiersSchema = v.record(v.string(), v.array(StoreSchema));

/** Every store the catalog knows, with its role. */
const listStores = query(async (): Promise<ApiResult<{ stores: Store[] }>> =>
	parsed(await catalogJSON('/management/v1/stores'), RegistrySchema),
);

/** The tier → store view, grouped by the catalog — derived there, so a store's tier cannot drift
 *  between the registry and the page that displays it. */
export const listStoresByTier = query(async (): Promise<ApiResult<Record<string, Store[]>>> =>
	parsed(await catalogJSON('/management/v1/stores/tiers'), TiersSchema),
);

/** Attach a bucket for BROWSING (registers only). Estate-admin gated by the catalog; echoes the
 *  WHOLE registry, and on success the active registry queries refresh in the same flight. */
export const attachStore = command(
	v.object({
		name: v.string(),
		bucket: v.string(),
		role: v.picklist(['raw', 'bronze', 'silver', 'gold', 'derived', 'observability']),
		endpoint: v.nullable(v.string()),
		description: v.string(),
	}),
	async (draft: StoreDraft): Promise<ApiResult<{ stores: Store[] }>> => {
		const result = parsed(
			await catalogJSON('/management/v1/stores', {
				method: 'POST',
				body: JSON.stringify({ ...draft, endpoint: draft.endpoint?.trim() || null }),
			}),
			RegistrySchema,
		);
		if (result.ok) {
			void listStores().refresh();
			void listStoresByTier().refresh();
		}
		return result;
	},
);
