import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '#lib/api/client.ts';
import type { CreateWarehouseBody, WarehouseRecord } from '../catalog';

import { catalogJSON, catalogListing, parsed } from '#lib/doors.ts';
import { childId, DELIMITER } from '#lib/api/identifiers.ts';
// Transport: the console's catalog door, `#lib/doors`.
// The warehouse + project registry, declared with `query`/`command` and answering the shared
// `ApiResult` union. The catalog's project-admin gate is enforced against a REAL user: these run in the
// browser and send only the signed-in session's bearer, with one named command per action.
//
// No local "signed out → 401" short-circuit: the catalog answers 401 itself for a bearer-less call,
// and the components branch on the STATUS.

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
	/** `"git"` for a warehouse git declares, whose lifecycle its doors refuse (XC-213); absent otherwise. */
	managed_by: v.optional(v.picklist(['git', 'self'])),
	delete_profile: v.optional(
		v.nullable(
			v.object({
				mode: v.picklist(['hard', 'soft', 'estate']),
				grace_days: v.optional(v.nullable(v.number())),
			}),
		),
	),
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
export const fetchWarehouses = query(async (): Promise<ApiResult<WarehouseRecord[]>> =>
	parsed(await catalogJSON('/management/v1/warehouses'), v.array(WarehouseSchema)),
);

/** One warehouse record — the hierarchy drill-down's warehouse page (can_get_metadata gated). */
export const fetchWarehouse = query(v.string(), async (id): Promise<ApiResult<WarehouseRecord>> =>
	parsed(await catalogJSON(`/management/v1/warehouses/${enc(id)}`), WarehouseSchema),
);

/** The spec's ListNamespaces answer: the child NAMES under the listed id, not their ids. */
const ChildNamesSchema = v.object({ namespaces: v.array(v.string()) });

/** The warehouses the caller may see, as the catalog's ROOT listing answers them (`GET
 *  /v1/namespace/$/list`, every page): each registered warehouse the caller holds `can_get_metadata`
 *  on, and the estate's own warehouse, which holds the lakehouse's own namespaces (`provenance`), has
 *  no registry record and belongs to no project. The registry (`fetchWarehouses`) lists the registered
 *  ones only, on the same relation, so a warehouse this answers and the registry does not is the
 *  estate's own. */
export const fetchServedWarehouses = query(async (): Promise<ApiResult<{ namespaces: string[] }>> =>
	parsed(
		// The root's id is the bare delimiter.
		await catalogListing(`/v1/namespace/${enc(DELIMITER)}/list`, 'namespaces'),
		ChildNamesSchema,
	),
);

/** The namespaces directly under one warehouse, as ids (`<warehouse>$<name>`): the spec's
 *  ListNamespaces on the warehouse's own name, every page, each item judged on the caller's grants.
 *  The same listing a warehouse delete reads for what it still holds, so an empty namespace is listed
 *  here as it is refused there. */
export const fetchWarehouseNamespaces = query(
	v.string(),
	async (id): Promise<ApiResult<{ namespaces: string[] }>> => {
		const result = parsed(
			await catalogListing(`/v1/namespace/${enc(id)}/list`, 'namespaces'),
			ChildNamesSchema,
		);
		return result.ok
			? { ok: true, data: { namespaces: result.data.namespaces.map((name) => childId(id, name)) } }
			: result;
	},
);

// This module reads no project: `/projects` is the estate's project list and `/projects/<p>` one
// project's reader, so the warehouse commands below refresh only the warehouse registry — a
// single-flight refresh of a query no surface holds is bytes for nobody.

/** Provision a warehouse — the create that MINTS a project when its `project` is new. Project-admin
 *  gated by the catalog (can_create_warehouse); on success both registry reads refresh in the same
 *  flight, so the gallery that issued the create never renders the estate it just changed. */
export const createWarehouse = command(
	({ id, project }) => `Create warehouse ${id} in project ${project}`,
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
		// `primary` is the operator's answer to an ambiguous project warehouse set, and it is defaulted
		// here for the same reason `protected` is: a warehouse is born non-primary and is designated
		// deliberately afterwards, so asking at create time would offer a choice that belongs to a
		// project already holding several warehouses. It must be PRESENT though — the generated body type
		// carries every field the server defaults, so valibot's inferred arg type has to carry it too or
		// the mismatch surfaces at the command's overload rather than anywhere near the cause.
		primary: v.optional(v.boolean(), false),
	}),
	async (body: CreateWarehouseBody): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON('/management/v1/warehouses', {
				method: 'POST',
				body: JSON.stringify(body),
			}),
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
	({ id, active }) => `${active ? 'Activate' : 'Deactivate'} warehouse ${id}`,
	v.object({ id: v.string(), active: v.boolean() }),
	async ({ id, active }): Promise<ApiResult<WarehouseRecord>> => {
		const result = parsed(
			await catalogJSON(
				`/management/v1/warehouses/${enc(id)}/${active ? 'activate' : 'deactivate'}`,
				{
					method: 'POST',
				},
			),
			WarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
		}
		return result;
	},
);

/** Delete a warehouse — the bottom-up destroy door (`DELETE /management/v1/warehouses/{id}`).
 *
 *  Three SEPARATE opt-ins that never share a default, sent only when the caller actually asked:
 *
 *  - `cascade` drops the namespaces under the warehouse. Without it a non-empty warehouse refuses 409
 *    with a detail that NAMES them — what this cascade would take, at the moment it was refused —
 *    which is why `WarehouseDeleteDialog` reads it out of the refusal.
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
	({ id, cascade, purgeBucket, force }) =>
		`${force ? 'Force-delete' : 'Delete'} warehouse ${id}${cascade ? ' with its namespaces' : ''}${purgeBucket ? ', purging its bucket' : ''}`,
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
			await catalogJSON(`/management/v1/warehouses/${enc(id)}${qs ? `?${qs}` : ''}`, {
				method: 'DELETE',
			}),
			DeleteWarehouseSchema,
		);
		if (result.ok) {
			void fetchWarehouses().refresh();
		}
		return result;
	},
);

/** Create a namespace directly under a warehouse: the spec's CreateNamespace on
 *  `<warehouse>$<name>`, mode `Create`, so a name that is already there is refused (409) rather than
 *  kept. The catalog judges it on the warehouse (`can_create_namespace`) and seeds its owner. On
 *  success the warehouse's listing refreshes in the same flight. */
export const createWarehouseNamespace = command(
	({ warehouse, name }) => `Create namespace ${name} in warehouse ${warehouse}`,
	v.object({ warehouse: v.string(), name: v.string() }),
	async ({ warehouse, name }): Promise<ApiResult<unknown>> => {
		const result = await catalogJSON(`/v1/namespace/${enc(childId(warehouse, name))}/create`, {
			method: 'POST',
			body: JSON.stringify({ mode: 'Create' }),
		});
		if (result.ok) void fetchWarehouseNamespaces(warehouse).refresh();
		return result;
	},
);
