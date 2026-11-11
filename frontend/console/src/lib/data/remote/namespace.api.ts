import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '#lib/api/client.ts';
import { DropNamespaceResponseSchema, type DropNamespace } from '../catalog';
import { fetchTables, TrashEntrySchema, type TrashEntry } from './catalog.api';
import { PolicyDescribeSchema, PolicyResponseSchema, type Policy } from '#lib/policies.ts';
import { PolicyDeleteResponseSchema, type NamespacePolicyDelete } from '../namespace';

import { catalogJSON, parsed } from '#lib/doors.ts';
// Transport: the console's catalog door, `#lib/doors`.
// The NAMESPACE-scoped lifecycle + maintenance-policy surfaces, declared with `query`/`command` and
// answering the shared `ApiResult` union. Owner-gated by the CATALOG (can_delete on namespace:<id>)
// against the signed-in user's own bearer.
//
// Every reply is valibot-parsed here (these responses are serialized with response_model_exclude_none,
// so nullable fields arrive absent): a contract drift is an honest `{ok:false, status:502}` the caller
// renders, never a cast.
//
// The wire contracts live in `../catalog` and `../namespace`, beside the types the components import.

const enc = encodeURIComponent;

/** The tables under ONE namespace — the namespace→table rung of the app Overview's hierarchy view
 *  (#109). The spec's list_tables through the catalog's GET route, reader-gated like every other read
 *  here.
 *
 *  Deliberately NOT `catalog.api.ts`'s `fetchTables`: that one reads the whole estate's registry in
 *  `<ns>$<table>` canonical form for the tables PAGE, so drawing a per-namespace rung from it would
 *  mean pulling every table in the estate to render one branch — and it would answer for namespaces
 *  this caller cannot read, which the hierarchy must show as "unreadable" rather than as empty. */
const NamespaceTablesSchema = v.object({ tables: v.array(v.string()) });
export const fetchNamespaceTables = query(
	v.string(),
	async (namespace): Promise<ApiResult<{ tables: string[] }>> =>
		parsed(await catalogJSON(`/v1/namespace/${enc(namespace)}/table/list`), NamespaceTablesSchema),
);

/** What a CALLER supplies to set a policy — the PolicyRequest wire body, every bound optional
 *  (absent = "the global default applies"), which is why the form omits an empty field rather than
 *  sending null. */
const PolicyDraftSchema = v.object({
	compact_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	retention_days: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
});

/** The namespace's maintenance policy (reader-gated), or null when none is set, which the caller
 *  renders as the honest "no policy, global defaults apply" state (never an error). */
export const fetchNamespacePolicy = query(
	v.object({ namespace: v.string() }),
	async ({ namespace }): Promise<ApiResult<Policy | null>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/namespace/${enc(namespace)}/policy/describe`, {
				method: 'POST',
			}),
			PolicyDescribeSchema,
		);
		return result.ok ? { ok: true, data: result.data.set ? result.data : null } : result;
	},
);

/** Set (or replace) the namespace-level maintenance policy — it governs every dataset under the
 *  namespace's prefix unless a table policy overrides it. Owner-gated (can_delete); on success the
 *  active describe refreshes in the same flight. */
export const setNamespacePolicy = command(
	({ namespace }) => `Set the maintenance policy of namespace ${namespace}`,
	v.object({ namespace: v.string(), policy: PolicyDraftSchema }),
	async ({ namespace, policy }): Promise<ApiResult<Policy>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/namespace/${enc(namespace)}/policy/set`, {
				method: 'POST',
				body: JSON.stringify(policy),
			}),
			PolicyResponseSchema,
		);
		if (result.ok) void fetchNamespacePolicy({ namespace }).refresh();
		return result;
	},
);

/** Remove the namespace's maintenance policy (idempotent) — owner-gated, same single-flight refresh. */
export const deleteNamespacePolicy = command(
	({ namespace }) => `Remove the maintenance policy of namespace ${namespace}`,
	v.object({ namespace: v.string() }),
	async ({ namespace }): Promise<ApiResult<NamespacePolicyDelete>> => {
		const result = parsed(
			await catalogJSON(`/management/v1/namespace/${enc(namespace)}/policy/delete`, {
				method: 'POST',
			}),
			PolicyDeleteResponseSchema,
		);
		if (result.ok) void fetchNamespacePolicy({ namespace }).refresh();
		return result;
	},
);

/** #85 drop a namespace. Cascade is a BODY field (`behavior: "Cascade"` also drops the tables inside;
 *  the default `"Restrict"` errors on a non-empty namespace) — not a query param. Owner-gated by the
 *  catalog (can_delete on namespace:<id>). */
export const dropNamespace = command(
	({ namespace, cascade }) =>
		cascade ? `Drop namespace ${namespace} and its tables` : `Drop namespace ${namespace}`,
	v.object({ namespace: v.string(), cascade: v.boolean() }),
	async ({ namespace, cascade }): Promise<ApiResult<DropNamespace>> => {
		const result = parsed(
			await catalogJSON(`/v1/namespace/${enc(namespace)}/drop`, {
				method: 'POST',
				body: JSON.stringify({ behavior: cascade ? 'Cascade' : 'Restrict' }),
			}),
			DropNamespaceResponseSchema,
		);
		// The registry IS the table list folded by namespace — a dropped namespace (and, on Cascade, its
		// tables) must leave it in the same flight, or the row the drop just removed re-renders intact.
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);

/** Whether the namespace exists for this caller — the spec's DescribeNamespace. */
export const describeNamespace = query(v.string(), async (namespace): Promise<ApiResult<unknown>> =>
	catalogJSON(`/v1/namespace/${enc(namespace)}/describe`, { method: 'POST', body: '{}' }),
);

/** The namespace's trash record: a recoverable drop the caller may see, or a 404 when there is none. */
export const fetchNamespaceTasks = query(
	v.string(),
	async (namespace): Promise<ApiResult<TrashEntry[]>> =>
		parsed(
			await catalogJSON(`/management/v1/namespace/${enc(namespace)}/tasks`),
			v.array(TrashEntrySchema),
		),
);
