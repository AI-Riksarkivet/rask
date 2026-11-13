/**
 * Every namespace directly under every warehouse the caller may see, through the spec's own listings:
 * the root's (the warehouses, `fetchServedWarehouses`), then each warehouse's (`fetchWarehouseNamespaces`).
 * No estate-wide namespace door exists, and none is needed: a namespace id begins with its warehouse,
 * so listing each warehouse once is the whole estate, one request per warehouse.
 *
 * A warehouse whose listing failed (a deactivated one answers 403) is named in `unread` with why, never
 * folded into "no namespaces": an empty namespace under it is then missing from the answer, and a page
 * says so.
 */
import type { ApiResult } from '#lib/api/client.ts';
import { readFailure } from './project-filter.ts';
import { fetchServedWarehouses, fetchWarehouseNamespaces } from './remote/warehouses.api.ts';

export type UnreadWarehouse = { warehouse: string; reason: string };

export type EstateNamespaces = {
	/** The warehouses the root listing answered, in its order. */
	warehouses: string[];
	/** The namespace ids under them, `<warehouse>$<name>`. */
	namespaces: string[];
	/** The warehouses whose own listing failed, and why. */
	unread: UnreadWarehouse[];
};

export async function listEstateNamespaces(): Promise<ApiResult<EstateNamespaces>> {
	const served = await fetchServedWarehouses();
	if (!served.ok) return served;
	const warehouses = served.data.namespaces;
	const listings = await Promise.all(warehouses.map((w) => fetchWarehouseNamespaces(w)));
	const namespaces: string[] = [];
	const unread: UnreadWarehouse[] = [];
	listings.forEach((listing, i) => {
		const warehouse = warehouses[i] ?? '';
		if (listing.ok) namespaces.push(...listing.data.namespaces);
		else
			unread.push({ warehouse, reason: readFailure(`warehouse ${warehouse} namespaces`, listing) });
	});
	return { ok: true, data: { warehouses, namespaces, unread } };
}
