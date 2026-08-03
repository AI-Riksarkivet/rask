// Typed client for the CATALOG service via the /capi BFF proxy (the /api proxy covers lineage).
// Types are generated from docs/catalog-openapi.json (`bun run gen:types:catalog`) — never hand-mirrored.
// The describe route serializes with response_model_exclude_none, so its null fields arrive absent —
// read optional fields with `?? null` rather than trusting the generated required-nullable shape.
import { parse } from '@rask/api';
import * as v from 'valibot';
import type { components } from '@rask/api/generated/catalog';
import { type ApiResult, requestBinary as requestBin, requestJSON as request } from '$lib/http';

export type TableDescribe = components['schemas']['DescribeTableResponse'];
export type TableStats = components['schemas']['GetTableStatsResponse'];
export type TableVersions = components['schemas']['ListTableVersionsResponse'];
export type TableTags = components['schemas']['ListTableTagsResponse'];
export type TableBranches = components['schemas']['ListTableBranchesResponse'];
export type TableIndexes = components['schemas']['ListTableIndicesResponse'];
export type Policy = components['schemas']['PolicyResponse'];
export type PolicyRequest = components['schemas']['PolicyRequest'];
export type Warehouse = components['schemas']['WarehouseResponse'];
export type CreateWarehouse = components['schemas']['CreateWarehouseRequest'];

/** A part the BFF could not resolve: `null` means genuinely absent (404 — e.g. no policy set),
 * `{ error }` means a transient upstream failure (5xx/403) — the page renders "unavailable" for the
 * latter, never an affirmative "none" that would invite an overwriting write. */
export type PartError = { error: number };
export function partErrored<T>(part: T | PartError | null): part is PartError {
	return part !== null && typeof part === 'object' && 'error' in part;
}

/** The detail-page aggregate the /capi/v1/table/{id}/detail BFF route assembles server-side —
 * `describe` gates the whole page (its failure is the page status), the rest are per-part optional. */
export type TableDetail = {
	describe: TableDescribe;
	stats: TableStats | PartError | null;
	versions: TableVersions | PartError | null;
	tags: TableTags | PartError | null;
	branches: TableBranches | PartError | null;
	indexes: TableIndexes | PartError | null;
	policy: Policy | PartError | null;
	format?: { name: string; storage_version: string }; // #78 the catalog's fixed Lance file format
};

/** Compatibility alias — the status-aware Result shape now lives in http.ts, shared with the lineage client. */
export type CatalogResult<T> = ApiResult<T>;

const requestJSON = <T>(path: string, init?: RequestInit) => request<T>('/capi', path, init);

const enc = encodeURIComponent;

export type AccessGrant = components['schemas']['AccessGrantResponse'];

/** #72 grant a base rung (owner/writer/reader/validator) to a subject on the table. `user` may be a bare
 * id (`alice`) or a userset (`role:…#assignee` / `team:…#member`). Owner-gated by the catalog (can_drop);
 * the BFF forwards only the signed-in user's session. */
export const grantTableAccess = (table: string, user: string, relation: string) =>
	requestJSON<AccessGrant>(`v1/table/${enc(table)}/access/grant`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ user, relation }),
	});
/** #72 revoke a base rung from a subject on the table — the write counterpart of the grant, same gate. */
export const revokeTableAccess = (table: string, user: string, relation: string) =>
	requestJSON<AccessGrant>(`v1/table/${enc(table)}/access/revoke`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ user, relation }),
	});

export type AccessGraph = components['schemas']['AccessGraphResponse'];

/** #81 one hop of the authorization graph around the table — its direct grantees + the parent edge, for the
 * SvelteFlow relationship explorer. Owner-gated by the catalog (can_drop); session-only BFF. */
export const fetchAccessGraph = (table: string) =>
	requestJSON<AccessGraph>(`v1/table/${enc(table)}/access/graph`, { method: 'POST' });

export type TablesList = components['schemas']['ListTablesResponse'];

/** The catalog's own table registry (#52) — names in `<ns>$<table>` canonical form. */
export const fetchTables = () => requestJSON<TablesList>('v1/table');

/** One-round-trip detail aggregate for the table page (schema/stats/versions/tags/policy). */
export const fetchTableDetail = (table: string) =>
	requestJSON<TableDetail>(`v1/table/${enc(table)}/detail`);

/** Preview: the first `limit` rows as an Arrow-IPC FILE (the catalog's query wire format), via this
 * zone's explicit POST BFF route (`{"limit": N}` — a confused-deputy enumerated read: the BFF builds
 * the catalog query itself and forwards nothing else). Reader-gated (can_read_data) at the catalog;
 * session-only BFF. The caller parses the bytes with apache-arrow. */
export const queryTableRows = (table: string, limit: number) =>
	requestBin('/capi', `v1/table/${enc(table)}/query`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ limit }),
	});

/** Maintenance-policy writes (#50 UI): owner-gated by the catalog (can_drop), session-only BFF. */
export const setTablePolicy = (table: string, policy: PolicyRequest) =>
	requestJSON<Policy>(`v1/table/${enc(table)}/policy`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(policy),
	});
export const deleteTablePolicy = (table: string) =>
	requestJSON<{ status: string }>(`v1/table/${enc(table)}/policy`, { method: 'DELETE' });

export type GcBounds = { retention_days?: number | null; retain_versions?: number | null };
export type GcPreview = components['schemas']['GcPreview'];
export type GcRunResult = components['schemas']['GcRunResult'];

/** #75 dry-run GC — the versions reclaimable under these bounds + the tags protecting others. Owner-gated
 * (can_drop), session-only BFF. Never mutates. */
export const previewMaintenance = (table: string, bounds: GcBounds) =>
	requestJSON<GcPreview>(`v1/table/${enc(table)}/maintenance/preview`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(bounds),
	});
/** #75 reclaim old versions on demand (destructive; tag-pinned versions exempt). Owner-gated. */
export const runMaintenance = (table: string, bounds: GcBounds) =>
	requestJSON<GcRunResult>(`v1/table/${enc(table)}/maintenance/run`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(bounds),
	});

export type CompactResult = components['schemas']['CompactResult'];

/** #76 compact small fragments on demand (non-destructive — writes a new version). Optional
 * target_rows_per_fragment overrides the sizing. Owner-gated (can_drop), session-only BFF. */
export const compactTable = (table: string, targetRowsPerFragment?: number | null) =>
	requestJSON<CompactResult>(`v1/table/${enc(table)}/maintenance/compact`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ target_rows_per_fragment: targetRowsPerFragment ?? null }),
	});

/** #64 version management — name (tag) a Lance version. Writer-gated (can_create_tag) by the catalog,
 * session-only BFF. A promotion pins its version with a tag; this is the manual equivalent. */
export const createTableTag = (table: string, tag: string, version: number) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/tags`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ tag, version }),
	});

/** #74 delete a tag (writer-gated). */
export const deleteTableTag = (table: string, tag: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/tags/delete`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ tag }),
	});
/** #74 move a tag to another version (owner-gated can_update_tag). */
export const moveTableTag = (table: string, tag: string, version: number) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/tags/update`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ tag, version }),
	});
/** #74 create a branch from a version (owner-gated can_create_branch). */
export const createTableBranch = (table: string, name: string, fromVersion?: number | null) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/branches/create`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ name, from_version: fromVersion ?? null }),
	});
/** #74 delete a branch (writer-gated). */
export const deleteTableBranch = (table: string, name: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/branches/delete`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ name }),
	});

/** #64 version management — restore the table to a prior version. Restore mints a FRESH version pointing
 * at the restored data (history is never rewritten); owner-gated (can_restore), session-only BFF. */
export const restoreTableVersion = (table: string, version: number) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/restore`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ version }),
	});

// #113 the commit log — `GET /v1/table/{id}/history?limit=N`, read out of the FORMAT (Lance's transaction
// log joined to `versions()`). The row shape is field-driven, not fixed: `version` / `timestamp` /
// `operation` always, then whichever of the transaction's own detail fields that operation carries
// (`dataplane._TXN_DETAIL_FIELDS`), list-valued ones collapsed to a count. Measured against the real dir
// backend: an `Overwrite` has NO `predicate` key at all (absent, not null), so this is a LOOSE object and
// the renderer works off the keys present — a per-operation switch would mislabel the operations Lance
// adds next. `additionalProperties: true` is all the generated types say, hence a hand-written schema.
const HistoryRowSchema = v.looseObject({
	version: v.number(),
	// Naive, offset-less, in the catalog PROCESS's local zone (measured: `tzinfo: None`). Never hand it to
	// `new Date()` — the browser would reinterpret it as local time. The `when` column uses the manifest's
	// `timestamp_millis` instead; this string is only ever shown verbatim.
	timestamp: v.optional(v.nullable(v.string())),
	// Lance's own vocabulary (`Overwrite`/`Append`/`Delete`/`Update`/`Merge`/`Rewrite`/`CreateIndex`/
	// `Restore`), NOT the catalog's (`create_table`/`insert`). `null` when the transaction file was
	// unreadable — the version still belongs in the log.
	operation: v.optional(v.nullable(v.string())),
	schema_set: v.optional(v.boolean()),
});
const TableHistorySchema = v.looseObject({
	table: v.optional(v.string()),
	versions: v.array(HistoryRowSchema),
});
export type HistoryRow = v.InferOutput<typeof HistoryRowSchema>;
export type TableHistory = v.InferOutput<typeof TableHistorySchema>;

/** #113 the table's commit log, newest first. Reader-gated at the catalog (`can_get_metadata`); a GET, so
 * it rides the existing `capi/v1/table/[id]/[...rest]` proxy with no route of its own. `limit` bounds the
 * transaction reads, so it is a real page size, not a client-side slice. */
export const fetchTableHistory = async (
	table: string,
	limit: number,
): Promise<ApiResult<TableHistory>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/history?limit=${limit}`);
	return res.ok ? { ok: true, data: parse(TableHistorySchema, res.data) } : res;
};

/** #64 data-plane row insert — append Arrow-IPC rows (built in the browser via apache-arrow). Writer-gated
 * (can_write_data) at the catalog, session-only BFF. `mode=append` never rewrites existing versions. */
export const insertRows = (table: string, arrow: Uint8Array) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/insert?mode=append`, {
		method: 'POST',
		headers: { 'content-type': 'application/vnd.apache.arrow.stream' },
		body: arrow as BodyInit,
	});

/** #74 schema evolution — add a SQL-expression column. Writer-gated (can_write_data), session-only BFF. */
export const addColumn = (table: string, name: string, expression: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/add`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ new_columns: [{ name, expression }] }),
	});
/** #74 rename an existing column (alter_columns path→rename). Writer-gated, session-only BFF. */
export const renameColumn = (table: string, path: string, rename: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/alter`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ alterations: [{ path, rename }] }),
	});
/** #74 drop a column. Writer-gated, session-only BFF. */
export const dropColumn = (table: string, name: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/drop`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ columns: [name] }),
	});

/** #74 tail — the scalar Arrow types the catalog's `_SCALAR_ARROW` map accepts as an alter_columns re-type
 * target. A cast Lance can't perform (e.g. string→int on non-numeric text) 400s at the catalog, surfaced. */
export const RETYPE_TYPES = [
	'int8',
	'int16',
	'int32',
	'int64',
	'uint8',
	'uint16',
	'uint32',
	'uint64',
	'float16',
	'float32',
	'float64',
	'string',
	'large_string',
	'bool',
	'date32',
	'date64',
	'binary',
	'large_binary',
] as const;

/** #74 tail — re-type an existing column (alter_columns path→data_type). Writer-gated, session-only BFF. */
export const retypeColumn = (table: string, path: string, type: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/alter`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ alterations: [{ path, data_type: { type } }] }),
	});

/** #74 tail — merge or replace one field's metadata (update_field_metadata). A `null` value deletes that key.
 * Writer-gated (can_write_data) at the catalog; session-only BFF. */
export const setFieldMetadata = (
	table: string,
	path: string,
	metadata: Record<string, string | null>,
	replace = false,
) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/field-meta`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ updates: [{ path, metadata, replace }] }),
	});

/** #74 tail — SET the table's schema-level metadata map (schema_metadata/update replaces the whole map, so
 * the caller sends the full desired map). Writer-gated (can_write_data) at the catalog; session-only BFF. */
export const setTableProperties = (table: string, metadata: Record<string, string>) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/columns/table-meta`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ metadata }),
	});

export type CreateIndexBody = { column: string; index_type: string; distance_type?: string };

/** #73 build an index — `scalar` picks the catalog's create_scalar_index (BTREE/BITMAP/INVERTED …) vs
 * create_index (the IVF/HNSW vector families). A rebuild is just a create with the same column (Lance
 * replaces the existing one). Writer-gated (can_write_data) at the catalog; session-only BFF. */
export const createTableIndex = (table: string, body: CreateIndexBody, scalar: boolean) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/index/create?scalar=${scalar ? 1 : 0}`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});
/** #73 drop a named index — writer-gated (can_write_data), session-only BFF. */
export const dropTableIndex = (table: string, name: string) =>
	requestJSON<unknown>(`v1/table/${enc(table)}/index/${enc(name)}/drop`, { method: 'POST' });

/** A warehouse record with the medallion serving class: `serving === "gold"` marks the project's
 * per-tenant SERVING warehouse (the gold tier's separate bucket — DECISIONS "Medallion tiers");
 * absent = a work warehouse. Additive over the generated shape until the spec regenerates. */
export type WarehouseRecord = Warehouse & { serving?: string | null };
/** The create body with the optional `serving: "gold"` class (same additive rationale). */
export type CreateWarehouseBody = CreateWarehouse & { serving?: 'gold' };

/** Warehouse admin (#3-A UI): reads for any signed-in user the catalog allows; writes are
 * project-admin gated by the catalog (can_create_warehouse / can_administer). */
export const fetchWarehouses = () => requestJSON<WarehouseRecord[]>('v1/warehouses');
/** One warehouse record — the hierarchy drill-down's warehouse page (can_get_metadata gated). */
export const fetchWarehouse = (id: string) =>
	requestJSON<WarehouseRecord>(`v1/warehouses/${enc(id)}`);

export type ProjectSummary = components['schemas']['ProjectResponse'];

/** The estate's tenants (estate-observer gated by the catalog — a member sees 403, handled). */
export const fetchProjects = () => requestJSON<ProjectSummary[]>('v1/projects');
/** One tenant: its warehouses + effective admins — the hierarchy drill-down's project page. */
export const fetchProject = (project: string) =>
	requestJSON<ProjectSummary>(`v1/projects/${enc(project)}`);
export const createWarehouse = (body: CreateWarehouseBody) =>
	requestJSON<WarehouseRecord>('v1/warehouses', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});

export const setWarehouseActive = (id: string, active: boolean) =>
	requestJSON<Warehouse>(`v1/warehouses/${enc(id)}/${active ? 'activate' : 'deactivate'}`, {
		method: 'POST',
	});
export const bindWarehouseNamespace = (id: string, namespace: string) =>
	requestJSON<unknown>(`v1/warehouses/${enc(id)}/namespaces`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ namespace }),
	});

// The lance-namespace table-lifecycle wire contracts (#85). All-optional where the spec says so — the
// catalog serializes with response_model_exclude_none, so null fields arrive absent. Parsed (not cast)
// at the boundary per the @rask/api parse-don't-validate rule: a schema drift throws to the caller
// instead of lying downstream.
const TableLifecycleResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.string()),
	id: v.optional(v.array(v.string())),
	location: v.optional(v.string()),
	properties: v.optional(v.record(v.string(), v.string())),
});
export type TableLifecycleResult = v.InferOutput<typeof TableLifecycleResponseSchema>;

const RenameTableResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.string()),
});

const DeclareTableResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.string()),
	location: v.optional(v.string()),
	storage_options: v.optional(v.record(v.string(), v.string())),
	properties: v.optional(v.record(v.string(), v.string())),
	managed_versioning: v.optional(v.boolean()),
});
export type DeclareTableResult = v.InferOutput<typeof DeclareTableResponseSchema>;

// UpdateTableResponse: updated_rows + version are REQUIRED on the wire (lance-namespace spec) — the
// affected-row count the UI surfaces. DeleteFromTableResponse carries NO row count, only the new version.
const UpdateRowsResponseSchema = v.object({
	transaction_id: v.optional(v.string()),
	updated_rows: v.number(),
	version: v.number(),
});
export type UpdateRowsResult = v.InferOutput<typeof UpdateRowsResponseSchema>;

const DeleteRowsResponseSchema = v.object({
	transaction_id: v.optional(v.string()),
	version: v.optional(v.number()),
});
export type DeleteRowsResult = v.InferOutput<typeof DeleteRowsResponseSchema>;

// AlterTableBackfillColumnsResponse: the backfill runs asynchronously — job_id is all it returns.
const BackfillResponseSchema = v.object({ job_id: v.string() });
export type BackfillResult = v.InferOutput<typeof BackfillResponseSchema>;

/** #85 drop the table (deletes data + revokes its grants). Owner-gated by the catalog (can_drop);
 * the BFF forwards only the signed-in user's session. */
export const dropTable = async (table: string): Promise<ApiResult<TableLifecycleResult>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/drop`, { method: 'POST' });
	return res.ok ? { ok: true, data: parse(TableLifecycleResponseSchema, res.data) } : res;
};

/** #85 deregister the table (detach from the catalog, data stays on storage). Owner-gated
 * (can_deregister); session-only BFF. */
export const deregisterTable = async (table: string): Promise<ApiResult<TableLifecycleResult>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/deregister`, { method: 'POST' });
	return res.ok ? { ok: true, data: parse(TableLifecycleResponseSchema, res.data) } : res;
};

/** #85 rename the table within its namespace (POSTs {new_table_name}; the catalog's in-process #5b
 * rename relocates the data + migrates FGA ownership). Gated can_drop on the source AND
 * can_create_table on the destination parent; session-only BFF. */
export const renameTable = async (
	table: string,
	newName: string,
): Promise<ApiResult<TableLifecycleResult>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/rename`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ new_table_name: newName }),
	});
	return res.ok ? { ok: true, data: parse(RenameTableResponseSchema, res.data) } : res;
};

/** #85 declare an empty table `<namespace>$<name>` — the browser-shaped create (JSON body, no Arrow);
 * the catalog reserves the id, seeds the caller's ownership, and emits the DECLARE_TABLE marker.
 * `location` optional (empty → the catalog picks). Gated can_create_table on the parent namespace;
 * session-only BFF. */
export const declareTable = async (
	namespace: string,
	name: string,
	location?: string,
): Promise<ApiResult<DeclareTableResult>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(`${namespace}$${name}`)}/declare`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(location ? { location } : {}),
	});
	return res.ok ? { ok: true, data: parse(DeclareTableResponseSchema, res.data) } : res;
};

/** #85 update rows matching a SQL predicate. `updates` is the wire's [[column, expression], …] SET
 * list (required); `predicate` optional (absent → all rows). Writer-gated (can_write_data);
 * session-only BFF. Returns the affected-row count + new version. */
export const updateRows = async (
	table: string,
	predicate: string | null,
	updates: [string, string][],
): Promise<ApiResult<UpdateRowsResult>> => {
	const body: { predicate?: string; updates: [string, string][] } = { updates };
	if (predicate) body.predicate = predicate;
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/update`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});
	return res.ok ? { ok: true, data: parse(UpdateRowsResponseSchema, res.data) } : res;
};

/** #85 delete rows matching a SQL predicate (required on the wire — there is no "delete all" default).
 * Writer-gated (can_write_data); session-only BFF. The response carries only the new version — the
 * wire has no deleted-row count. */
export const deleteRows = async (
	table: string,
	predicate: string,
): Promise<ApiResult<DeleteRowsResult>> => {
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/delete`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ predicate }),
	});
	return res.ok ? { ok: true, data: parse(DeleteRowsResponseSchema, res.data) } : res;
};

/** #85 backfill values into a column (async native job — the response is a job_id, and the version
 * bump is reconciled when the job lands). Optional `where` bounds the backfill. Writer-gated
 * (can_write_data); session-only BFF via the columns allowlist route. */
export const backfillColumn = async (
	table: string,
	column: string,
	where?: string,
): Promise<ApiResult<BackfillResult>> => {
	const body: { column: string; where?: string } = { column };
	if (where) body.where = where;
	const res = await requestJSON<unknown>(`v1/table/${enc(table)}/columns/backfill`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});
	return res.ok ? { ok: true, data: parse(BackfillResponseSchema, res.data) } : res;
};

// The lance-namespace DropNamespaceResponse wire contract (all fields optional — the catalog serializes
// with response_model_exclude_none). Parsed (not cast) at the boundary per the @rask/api
// parse-don't-validate rule: a schema drift throws to the caller instead of lying downstream.
const DropNamespaceResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	properties: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.array(v.string())),
});
export type DropNamespace = v.InferOutput<typeof DropNamespaceResponseSchema>;

/** #85 drop a namespace. Cascade is a BODY field (`behavior: "Cascade"` also drops the tables inside;
 * the default `"Restrict"` errors on a non-empty namespace) — not a query param. Owner-gated by the
 * catalog (can_delete on namespace:<id>); the BFF forwards only the signed-in user's session. */
export const dropNamespace = async (
	namespace: string,
	cascade: boolean,
): Promise<ApiResult<DropNamespace>> => {
	const res = await requestJSON<unknown>(`v1/namespace/${enc(namespace)}/drop`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ behavior: cascade ? 'Cascade' : 'Restrict' }),
	});
	return res.ok ? { ok: true, data: parse(DropNamespaceResponseSchema, res.data) } : res;
};
