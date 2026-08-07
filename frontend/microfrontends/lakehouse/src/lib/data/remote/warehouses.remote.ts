import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { CreateWarehouseBody, WarehouseRecord } from '../catalog';

import { catalogJSON, parsed } from '$lib/server/doors';
// Transport: the zone's ONE catalog door (#93) — implementation in @rask/api/upstream.
// The warehouse + project registry, in the zone's remote-function dialect — same
// names, same `ApiResult` shapes as the /capi client this replaces, transport only. The three narrow
// BFF routes it retires (`capi/v1/warehouses`, `capi/v1/warehouses/[id]/[action]`) existed to keep the
// catalog's project-admin gate enforced against a REAL user rather than a service credential; that
// stance is unchanged here — a remote function runs on the zone server and forwards only the signed-in
// session's bearer, and the action allowlist dissolves into three named commands.
//
// The routes' `authEnabled && !session → 401` short-circuit is gone deliberately: it was a UX
// shortcut, never the enforcement point (the catalog answers 401 itself for a bearer-less call), and
// the components branch on the STATUS, which is identical either way.

const enc = encodeURIComponent;

/** `WarehouseResponse` + the additive `serving` class (see `WarehouseRecord` in ../catalog). */
const WarehouseSchema = v.object({
	id: v.string(),
	project: v.string(),
	bucket: v.string(),
	root_uri: v.string(),
	created_at: v.optional(v.nullable(v.string())),
	serving: v.optional(v.nullable(v.string())),
	status: v.optional(v.nullable(v.string())),
});

/** `DeleteWarehouseResponse` — what the delete ACTUALLY did, store by store.
 *
 *  Every field is an OBSERVED outcome, never an echo of the request, and that is the whole reason the
 *  catalog returns a body at all: a warehouse delete touches four independent stores (the native
 *  namespaces, OpenFGA, the registry, the bucket) and only three of them run by default. `bucket` is
 *  named even when it was KEPT, so the operator knows what survived; `bucket_purged` is the catalog's
 *  own statement about the bytes, which is the one field a request-shaped message gets wrong. The
 *  confirm dialog's toast is built from this and nothing else. */
const DeleteWarehouseSchema = v.object({
	id: v.string(),
	bucket: v.string(),
	namespaces_dropped: v.array(v.string()),
	tuples_revoked: v.number(),
	bucket_purged: v.boolean(),
	objects_purged: v.number(),
});




/** Warehouse admin reads: whatever the catalog shows this caller (any signed-in user). */
export const fetchWarehouses = query(
	async (): Promise<ApiResult<WarehouseRecord[]>> =>
		parsed(await catalogJSON('/v1/warehouses'), v.array(WarehouseSchema)),
);

/** One warehouse record — the hierarchy drill-down's warehouse page (can_get_metadata gated). */
export const fetchWarehouse = query(
	v.string(),
	async (id): Promise<ApiResult<WarehouseRecord>> =>
		parsed(await catalogJSON(`/v1/warehouses/${enc(id)}`), WarehouseSchema),
);

/** `EstateBindingsResponse` — `{namespace: warehouse_id}` for the whole estate, in ONE catalog read.
 *  Replaces the 1+N fan-out the namespaces page used to do (fetchWarehouses, then one
 *  fetchWarehouseNamespaces per warehouse) — and each of those legs re-read EVERY binding in the
 *  estate server-side before filtering, so the union cost O(warehouses × bindings) to compute
 *  something `list_bindings` produces in a single pass. */
const EstateBindingsSchema = v.object({ bindings: v.record(v.string(), v.string()) });

export const fetchEstateBindings = query(
	async (): Promise<ApiResult<{ bindings: Record<string, string> }>> =>
		parsed(await catalogJSON('/v1/warehouses/-/bindings'), EstateBindingsSchema),
);

/** `WarehouseNamespacesResponse` — the spec's ListNamespaces shape (`{"namespaces": [...]}`). */
const WarehouseNamespacesSchema = v.object({ namespaces: v.array(v.string()) });

/** The namespaces BOUND to one warehouse, from the REGISTRY — the same source the delete door
 *  consults (#66). This is the FACT the warehouse page renders; any table-derived view is at best an
 *  enrichment on top of it, because an empty namespace holds zero tables and is exactly what the
 *  delete door will refuse on. */
export const fetchWarehouseNamespaces = query(
	v.string(),
	async (id): Promise<ApiResult<{ namespaces: string[] }>> =>
		parsed(await catalogJSON(`/v1/warehouses/${enc(id)}/namespaces`), WarehouseNamespacesSchema),
);

// The PROJECT reads are GONE from this zone, both of them. `fetchProject` (one tenant) went with
// `catalog/projects/[project]` when the per-project overview moved to the home zone; the estate's
// tenant LIST went with the Tenants panel, which the #105 estate-settings port retired outright —
// `/projects` in the home zone IS the estate's project list, and a second one under a project-scoped
// zone's catalog was the duplication the 2026-08-03 ruling deletes. Nothing here renders a project
// any more, so the warehouse commands below refresh only the warehouse registry: a single-flight
// refresh of a query no surface holds is bytes for nobody.

/** Provision a warehouse — the create that MINTS a project when its `project` is new. Project-admin
 *  gated by the catalog (can_create_warehouse); on success both registry reads refresh in the same
 *  flight, so the gallery that issued the create never renders the estate it just changed. */
export const createWarehouse = command(
	v.object({
		id: v.string(),
		project: v.string(),
		bucket: v.optional(v.nullable(v.string())),
		serving: v.optional(v.literal('gold')),
		// #73 made `protected` REQUIRED on `CreateWarehouseBody` — protection now covers every rung of
		// the hierarchy, not just tables. Defaulted rather than surfaced: a warehouse is born
		// unprotected and is armed deliberately afterwards through the protection door, so asking at
		// create time would offer a choice the estate does not actually want made there. Omitting it
		// entirely is what broke the build — valibot's inferred arg type then lacked a field the
		// generated body type requires, and the mismatch surfaced at the command's overload rather
		// than anywhere near the cause.
		protected: v.optional(v.boolean(), false),
	}),
	async (body: CreateWarehouseBody): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON('/v1/warehouses', { method: 'POST', body: JSON.stringify(body) }),
			WarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
		}
		return result;
	},
);

/** The activate/deactivate lifecycle — one command instead of the route's action allowlist. */
export const setWarehouseActive = command(
	v.object({ id: v.string(), active: v.boolean() }),
	async ({ id, active }): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON(`/v1/warehouses/${enc(id)}/${active ? 'activate' : 'deactivate'}`, {
				method: 'POST',
			}),
			WarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
		}
		return result;
	},
);

/** Delete a warehouse — the bottom-up destroy door (`DELETE /v1/warehouses/{id}`).
 *
 *  Three SEPARATE opt-ins that never share a default, sent only when the caller actually asked:
 *
 *  - `cascade` drops the namespaces bound to the warehouse. Without it a non-empty warehouse refuses
 *    409 with a detail that NAMES them — the only authoritative statement of what a cascade would take
 *    (there is no bindings read API), which is why `WarehouseDeleteDialog` reads it out of the refusal.
 *  - `purgeBucket` is the ONLY thing that removes bytes, and it is irreversible. A catalog record is
 *    recoverable and a customer's bucket is not, so the dialog gates it behind typing the bucket name.
 *  - `force` overrides the `protected` flag and NOTHING else. The FGA gate has already run, identically,
 *    with or without it — two independent locks, and force turns exactly one.
 *
 *  A 404 here is deliberately ambiguous (no existence oracle): "no such warehouse" and "not yours" are
 *  made indistinguishable at the catalog, so the caller cannot probe which warehouse ids exist. The
 *  dialog surfaces that as-is rather than guessing which one it was.
 *
 *  On success both registry reads refresh in the SAME flight, like every other write in this module —
 *  the gallery that issued the delete never renders an estate it just changed. */
export const deleteWarehouse = command(
	v.object({
		id: v.string(),
		cascade: v.optional(v.boolean()),
		purgeBucket: v.optional(v.boolean()),
		force: v.optional(v.boolean()),
	}),
	async ({
		id,
		cascade,
		purgeBucket,
		force,
	}): Promise<ApiResult<v.InferOutput<typeof DeleteWarehouseSchema>>> => {
		// Omitted, not sent as `false`: the catalog defaults all three to false, and a query string that
		// spells the falses out is one copy-paste away from spelling one of them true.
		const q = new URLSearchParams();
		if (cascade) q.set('cascade', 'true');
		if (purgeBucket) q.set('purge_bucket', 'true');
		if (force) q.set('force', 'true');
		const qs = q.toString();
		const result = parsed(
			await catalogJSON(`/v1/warehouses/${enc(id)}${qs ? `?${qs}` : ''}`, { method: 'DELETE' }),
			DeleteWarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
		}
		return result;
	},
);

/** Bind a namespace to a warehouse (bucket-per-warehouse tenancy). The response is not rendered —
 *  the banner reports the outcome — so it is passed through unparsed. The registry read refreshes
 *  rather than nothing at all: an explicit single-flight refresh keeps SvelteKit from falling back to
 *  invalidating every query on the page. */
export const bindWarehouseNamespace = command(
	v.object({ id: v.string(), namespace: v.string() }),
	async ({ id, namespace }): Promise<ApiResult<unknown>> => {
		const result = await catalogJSON(`/v1/warehouses/${enc(id)}/namespaces`, {
			method: 'POST',
			body: JSON.stringify({ namespace }),
		});
		if (result.ok) void fetchWarehouses().refresh();
		return result;
	},
);
