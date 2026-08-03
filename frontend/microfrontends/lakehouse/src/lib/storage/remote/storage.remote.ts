import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import type { Store, StoreDraft } from '../storage';

// The stores registry, in the zone's remote-function dialect (open_transport.md, area 1) — same
// names, same ApiResult shapes as the /capi client this replaces, transport only. Fixing one live
// defect for free: `attachStore` used to POST through the GET-only /capi catch-all, a guaranteed 405
// that left the attach form dead; a command() has its own endpoint.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const StoreSchema = v.object({
	name: v.string(),
	bucket: v.string(),
	role: v.picklist(['raw', 'bronze', 'silver', 'gold', 'derived', 'observability']),
	description: v.string(),
	read_only: v.boolean(),
});
const RegistrySchema = v.object({ stores: v.array(StoreSchema) });
const TiersSchema = v.record(v.string(), v.array(StoreSchema));

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	// An UNREACHABLE catalog is `{ok:false, status:0}`, exactly like the browser client this replaced —
	// a rejected fetch here would throw across the remote boundary and skip every consumer's honest
	// offline/status-0 branch (e.g. failText's "catalog unreachable / may-or-may-not-have-applied").
	let res: Response;
	try {
		res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
	} catch (err) {
		return { ok: false, status: 0, detail: String(err) };
	}
	if (!res.ok) {
		let detail = `catalog answered ${res.status}`;
		try {
			const body: unknown = await res.json();
			if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
		} catch {
			/* a non-JSON error body keeps the status-line detail */
		}
		return { ok: false, status: res.status, detail };
	}
	return { ok: true, data: await res.json() };
}

function parsed<T>(result: ApiResult<unknown>, schema: v.GenericSchema<unknown, T>): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}

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
