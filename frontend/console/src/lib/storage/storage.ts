// The stores registry's shapes. The transport (list / tiers / attach) lives in `remote/storage.api.ts`.

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

/** What the attach form collects. `read_only` is absent on purpose — the catalog forces it true, so
 *  offering the choice here would be a control that does nothing. */
export type StoreDraft = {
	name: string;
	bucket: string;
	role: StorageRole;
	endpoint: string | null;
	description: string;
};
