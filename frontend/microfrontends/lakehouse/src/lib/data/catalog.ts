// Typed client for the CATALOG service via the /capi BFF proxy (the /api proxy covers lineage).
// Types are generated from docs/catalog-openapi.json (`bun run gen:types:catalog`) — never hand-mirrored.
// The describe route serializes with response_model_exclude_none, so its null fields arrive absent —
// read optional fields with `?? null` rather than trusting the generated required-nullable shape.
//
// The table-LIFECYCLE transport (registry list, detail aggregate, policy, GC/compaction, tags,
// branches, restore, schema evolution, indexes, drop/deregister/rename/declare, row update/delete)
// lives in `remote/catalog.remote.ts` — the zone's remote-function dialect. What stays here: the wire
// CONTRACTS (types + the valibot schemas that module parses with) and the clients whose routes are
// keep-bytes by verdict — the Arrow preview/insert and the commit log, which ride the `[id]/[...rest]`
// proxy that also serves blob `<img>` bytes.
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

/** The detail-page aggregate `fetchTableDetail` assembles server-side (six POST-shaped catalog reads
 * the browser could never issue through a GET proxy) — `describe` gates the whole page (its failure is
 * the page status), the rest are per-part optional. */
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

// The TABLE-scoped access surfaces (#72 grant/revoke, #81 graph) moved to remote functions with the
// namespace ones they duplicated — `remote/access-objects.remote.ts`, kind-generalized over
// 'table' | 'namespace' (the transport ruling, area 1). Their contracts live in `./namespace`.

export type TablesList = components['schemas']['ListTablesResponse'];

// The registry list (#52, `<ns>$<table>` names) and the one-round-trip detail aggregate are remote
// functions now — `remote/catalog.remote.ts`. The aggregate's server-side fan-out is unchanged; only
// the transport under it is.

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

// Policy set/delete (#50), the GC preview/run pair (#75), compaction (#76) and the version REFS —
// tags, branches, restore (#64/#74) — are remote functions now (`remote/catalog.remote.ts`). Their
// bodies are unchanged; these types are what both sides read them as.
export type GcBounds = { retention_days?: number | null; retain_versions?: number | null };
export type GcPreview = components['schemas']['GcPreview'];
export type GcRunResult = components['schemas']['GcRunResult'];
export type CompactResult = components['schemas']['CompactResult'];

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

// Schema evolution (#74: add / rename / drop / re-type a column, the field- and table-level property
// writes) and index build/drop (#73) are remote functions now — `remote/catalog.remote.ts`. The
// `[op]` allowlist route they shared dissolved into one function per operation, which is why the
// prototype-key guard it needed is gone with it.

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

export type CreateIndexBody = { column: string; index_type: string; distance_type?: string };

/** A warehouse record with the medallion serving class: `serving === "gold"` marks the project's
 * per-tenant SERVING warehouse (the gold tier's separate bucket — DECISIONS "Medallion tiers");
 * absent = a work warehouse. Additive over the generated shape until the spec regenerates. */
export type WarehouseRecord = Warehouse & { serving?: string | null };
/** The create body with the optional `serving: "gold"` class (same additive rationale). */
export type CreateWarehouseBody = CreateWarehouse & { serving?: 'gold' };

export type ProjectSummary = components['schemas']['ProjectResponse'];

// The warehouse + project TRANSPORT (list / describe / create / activate / bind) lives in
// `remote/warehouses.remote.ts` — the zone's remote-function dialect. This module keeps the table
// surface plus the shared wire contracts, which a remote file may not export.

// The lance-namespace table-lifecycle wire contracts (#85). All-optional where the spec says so — the
// catalog serializes with response_model_exclude_none, so null fields arrive absent. Parsed (not cast)
// at the boundary per the @rask/api parse-don't-validate rule: a schema drift is surfaced instead of
// lying downstream. The boundary itself moved to the zone server (`remote/catalog.remote.ts`) with the
// transport, so these are exported — the contract stays here, beside the types it defines.
export const TableLifecycleResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.string()),
	id: v.optional(v.array(v.string())),
	location: v.optional(v.string()),
	properties: v.optional(v.record(v.string(), v.string())),
});
export type TableLifecycleResult = v.InferOutput<typeof TableLifecycleResponseSchema>;

export const RenameTableResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.string()),
});

export const DeclareTableResponseSchema = v.object({
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
export const UpdateRowsResponseSchema = v.object({
	transaction_id: v.optional(v.string()),
	updated_rows: v.number(),
	version: v.number(),
});
export type UpdateRowsResult = v.InferOutput<typeof UpdateRowsResponseSchema>;

export const DeleteRowsResponseSchema = v.object({
	transaction_id: v.optional(v.string()),
	version: v.optional(v.number()),
});
export type DeleteRowsResult = v.InferOutput<typeof DeleteRowsResponseSchema>;

// AlterTableBackfillColumnsResponse: the backfill runs asynchronously — job_id is all it returns.
export const BackfillResponseSchema = v.object({ job_id: v.string() });
export type BackfillResult = v.InferOutput<typeof BackfillResponseSchema>;

// Every function that used to live here — drop / deregister / rename / declare, the row update and
// delete, and the async column backfill — is a remote command now (`remote/catalog.remote.ts`). The
// parse against the schemas above moved with them, from the browser to the zone server.

// The lance-namespace DropNamespaceResponse wire contract (all fields optional — the catalog serializes
// with response_model_exclude_none). Parsed (not cast) at the boundary per the @rask/api
// parse-don't-validate rule: a schema drift throws to the caller instead of lying downstream.
export const DropNamespaceResponseSchema = v.object({
	context: v.optional(v.record(v.string(), v.string())),
	properties: v.optional(v.record(v.string(), v.string())),
	transaction_id: v.optional(v.array(v.string())),
});
export type DropNamespace = v.InferOutput<typeof DropNamespaceResponseSchema>;
// The drop itself is a remote command — `remote/namespace.remote.ts` (the transport ruling, area 1).
