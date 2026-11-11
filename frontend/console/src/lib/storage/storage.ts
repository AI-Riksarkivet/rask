// The stores registry's shapes. The read lives in `remote/storage.api.ts`.

/** Where a store sits in the cascade. The three GOVERNED tiers are exactly bronze/silver/gold (R23) —
 *  raw is the external world, never a tier, and derived/observability sit alongside the medallion. */
export type StorageRole = 'raw' | 'bronze' | 'silver' | 'gold' | 'derived' | 'observability';

/** One registered object store. */
export type Store = {
	name: string;
	bucket: string;
	role: StorageRole;
	description: string;
	read_only: boolean;
};
