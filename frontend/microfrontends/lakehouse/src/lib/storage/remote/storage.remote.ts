import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { Store, StoreDraft } from '../storage';

import { catalogJSON, parsed } from '$lib/server/doors';
// Transport: the zone's ONE catalog door (#93) — implementation in @rask/api/upstream.
// The stores registry, in the zone's remote-function dialect (the transport ruling, area 1) — same
// names, same ApiResult shapes as the /capi client this replaces, transport only. Fixing one live
// defect for free: `attachStore` used to POST through the GET-only /capi catch-all, a guaranteed 405
// that left the attach form dead; a command() has its own endpoint.

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
export const listStores = query(
	async (): Promise<ApiResult<{ stores: Store[] }>> =>
		parsed(await catalogJSON('/v1/stores'), RegistrySchema),
);

/** The tier → store view, grouped by the catalog — derived there, so a store's tier cannot drift
 *  between the registry and the page that displays it. */
export const listStoresByTier = query(
	async (): Promise<ApiResult<Record<string, Store[]>>> =>
		parsed(await catalogJSON('/v1/stores/tiers'), TiersSchema),
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
			await catalogJSON('/v1/stores', {
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
