import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import {
	BackfillResponseSchema,
	DeclareTableResponseSchema,
	DeleteRowsResponseSchema,
	RenameTableResponseSchema,
	TableLifecycleResponseSchema,
	UpdateRowsResponseSchema,
	type BackfillResult,
	type CompactResult,
	type CreateIndexBody,
	type DeclareTableResult,
	type DeleteRowsResult,
	type GcBounds,
	type GcPreview,
	type GcRunResult,
	type Policy,
	type TableDetail,
	type TableLifecycleResult,
	type TablesList,
	type UpdateRowsResult,
} from '../catalog';

// The catalog TABLE-LIFECYCLE plane, in the zone's remote-function dialect (the transport ruling, area 1)
// — same function names, same `ApiResult` shapes at every call site, transport only. Seventeen `/capi`
// routes (the copy-pasted bearer-forward template, three of them with an action allowlist) collapse
// into the named functions below: the allowlists dissolve into function identity, and the columns
// route's `Object.hasOwn` prototype-key guard becomes unnecessary because there is no longer a map to
// index with a client-supplied string.
//
// The confused-deputy stance is UNCHANGED: these run on the zone (SvelteKit/Bun) server and forward
// ONLY the signed-in user's bearer, so the catalog's own gate (can_drop / can_write_data / …) is
// enforced against a real user. The deleted routes ALSO refused an anonymous visitor locally on an
// OIDC tier; that refusal is now the catalog's 401, which every caller already renders the same way
// (each has its own "Sign in to …" copy for status 401).
//
// PARSING follows the same rule the routes did — every parse that existed moves server-side, and none
// is invented. The lance-namespace lifecycle contracts (drop/deregister/rename/declare/update/delete/
// backfill) were valibot-parsed in the browser and are parsed HERE now; the OpenAPI-generated shapes
// (detail aggregate, policy, GC, compaction, tags, branches, indexes) were passed through untyped and
// still are — hand-mirroring six generated schemas in valibot would be a second source of truth.
//
// SINGLE-FLIGHT REFRESH, where it pays: the four writes that change WHICH TABLES EXIST
// (declare/drop/deregister/rename) refresh `fetchTables()` in the same flight. The detail aggregate
// is deliberately NOT refreshed by its own writes — every caller of those commands already re-reads
// it through the page's `load()` (that is what makes the version bump visible), so refreshing it
// here would buy nothing and cost a second six-read fan-out per write.
//
// A remote file may export only remote functions, so the wire contracts stay in `../catalog`.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const enc = encodeURIComponent;

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** One catalog call → `ApiResult<unknown>`; FastAPI's `{detail}` is surfaced as the failure detail. An
 *  unreachable catalog is a 502 rather than a thrown error — the deleted routes each ended in
 *  `catch (err) { return json({detail}, 502) }`, and the pages read that status to render "Catalog
 *  unreachable" instead of an affirmative empty state. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
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
		return { ok: false, status: 502, detail: String(err) };
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

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
function parsed<T>(result: ApiResult<unknown>, schema: v.GenericSchema<unknown, T>): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}

/** The generated-shape passthrough: the catalog's OpenAPI type IS the contract for these, so the
 *  payload is handed on with the type the client already used (exactly what the routes did). */
function typedAs<T>(result: ApiResult<unknown>): ApiResult<T> {
	return result.ok ? { ok: true, data: result.data as T } : result;
}

/** A POST-shaped catalog write with a JSON body. */
const post = (path: string, body: unknown): Promise<ApiResult<unknown>> =>
	catalogJSON(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

const TableArg = v.object({ table: v.string() });

// The maintenance-policy body (#50) — every field optional so the caller sends exactly the bounds it
// set, and every bound nullable because the generated `PolicyRequest` says so: an explicit `null` is
// "no bound", which the catalog reads differently from an absent key.
const PolicySchema = v.object({
	compact_enabled: v.optional(v.boolean()),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	retention_days: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
});

// #75 GC bounds: both nullable — "any age" / "keep everything" are real answers, not absences.
const GcBoundsSchema = v.object({
	retention_days: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
});

// ── reads ──────────────────────────────────────────────────────────────────────────────────────

/** The catalog's own table registry (#52) — names in `<ns>$<table>` canonical form. Its route existed
 *  only because the static `[id]` branch shadowed the `/capi` catch-all for the bare list; a remote
 *  function has no such shadowing. */
export const fetchTables = query(
	async (): Promise<ApiResult<TablesList>> => typedAs<TablesList>(await catalogJSON('/v1/table')),
);

/** `TrashEntry[]` — what is SCHEDULED against one table (#75/§2.4). Today that is exactly one thing:
 *  a pending undrop deadline. Reader-tier at the catalog, so an owner can see the clock they are
 *  racing — a deadline nobody can read is not a safety feature. */
const TrashEntrySchema = v.object({
	id: v.string(),
	location: v.string(),
	dropped_by: v.string(),
	dropped_at: v.string(),
	expires_at: v.string(),
});
export type TrashEntry = v.InferOutput<typeof TrashEntrySchema>;

export const fetchTableTasks = query(
	v.string(),
	async (table): Promise<ApiResult<TrashEntry[]>> =>
		parsed(await catalogJSON(`/v1/table/${enc(table)}/tasks`), v.array(TrashEntrySchema)),
);

/** Recover a dropped table from the trash (#75). Owner-gated at the catalog; a 404 means the grace
 *  period expired or the drop purged its bytes — genuinely unrecoverable, and the UI says so rather
 *  than pretending. Single-flights the table list so the recovered table reappears. */
export const undropTable = command(v.string(), async (table): Promise<ApiResult<unknown>> => {
	const res = await catalogJSON(`/v1/table/${enc(table)}/undrop`, { method: 'POST' });
	if (res.ok) {
		// BOTH reads, and the detail one is load-bearing: the page offering Undrop is showing a CACHED
		// 404 for this very table, so refreshing only the list left the recovered table still rendering
		// "not a catalog-registered table" until a hard reload — seen in the browser, not reasoned about.
		void fetchTables().refresh();
		void fetchTableDetail({ table }).refresh();
	}
	return res;
});

/** One-round-trip detail aggregate for the table page (schema/stats/versions/tags/branches/indexes/
 *  policy). The fan-out stays SERVER-side, exactly as the BFF route ran it — six POST-shaped catalog
 *  reads the browser could never issue through a GET proxy.
 *
 *  Absence vs failure is kept distinct per part: a 404 becomes `null` (genuinely unset — e.g. no
 *  policy), a 5xx/403 becomes `{ error: status }` so the page renders "unavailable" instead of an
 *  affirmative "no policy" that would invite an overwriting write. `describe` gates the whole page:
 *  its status IS the page's status. */
export const fetchTableDetail = query(
	TableArg,
	async ({ table }): Promise<ApiResult<TableDetail>> => {
		const { fetch } = getRequestEvent();
		const base = `${CATALOG_API}/v1/table/${enc(table)}`;
		const headers = bearerHeaders();
		const read = (path: string) => fetch(`${base}/${path}`, { method: 'POST', headers });
		const part = async (res: Response): Promise<unknown> => {
			if (res.ok) return res.json();
			return res.status === 404 ? null : { error: res.status };
		};
		try {
			const describe = await read('describe?load_detailed_metadata=true&with_table_uri=true');
			if (!describe.ok) {
				let detail = `catalog answered ${describe.status}`;
				try {
					const body: unknown = await describe.json();
					if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
				} catch {
					/* a non-JSON error body keeps the status-line detail */
				}
				return { ok: false, status: describe.status, detail };
			}
			const [stats, versions, tags, branches, indexes, policy] = await Promise.all([
				read('stats').then(part),
				read('version/list').then(part),
				read('tags/list').then(part),
				read('branches/list').then(part),
				read('index/list').then(part),
				read('policy/describe').then(part),
			]);
			return {
				ok: true,
				data: {
					describe: await describe.json(),
					stats,
					versions,
					tags,
					branches,
					indexes,
					policy,
					// #78 format is a fixed property of this catalog: it stores Lance datasets ONLY
					// (columnar, self-describing, versioned), pinned to storage format 2.2 at create
					// (dataplane.py data_storage_version="2.2"). Surfaced so the one supported format is
					// discoverable, and the create path 400s a client that tries to select another.
					format: { name: 'Lance', storage_version: '2.2' },
				} as TableDetail,
			};
		} catch (err) {
			return { ok: false, status: 502, detail: String(err) };
		}
	},
);

/** #75 dry-run GC — the versions reclaimable under these bounds + the tags protecting others.
 *  Owner-gated (can_drop). A POST at the catalog, a READ here: it never mutates. */
export const previewMaintenance = query(
	v.object({ table: v.string(), bounds: GcBoundsSchema }),
	async ({ table, bounds }: { table: string; bounds: GcBounds }): Promise<ApiResult<GcPreview>> =>
		typedAs<GcPreview>(await post(`/v1/table/${enc(table)}/maintenance/preview`, bounds)),
);

// ── maintenance + policy writes ────────────────────────────────────────────────────────────────

/** Maintenance-policy set (#50 UI) — idempotent replace, owner-gated by the catalog (can_drop). */
export const setTablePolicy = command(
	v.object({ table: v.string(), policy: PolicySchema }),
	async ({ table, policy }): Promise<ApiResult<Policy>> =>
		typedAs<Policy>(await post(`/v1/table/${enc(table)}/policy/set`, policy)),
);

/** Maintenance-policy delete — idempotent removal, same owner gate. */
export const deleteTablePolicy = command(
	TableArg,
	async ({ table }): Promise<ApiResult<{ status: string }>> =>
		typedAs<{ status: string }>(
			await catalogJSON(`/v1/table/${enc(table)}/policy/delete`, { method: 'POST' }),
		),
);

/** #75 reclaim old versions on demand (destructive; tag-pinned versions exempt). Owner-gated. */
export const runMaintenance = command(
	v.object({ table: v.string(), bounds: GcBoundsSchema }),
	async ({ table, bounds }: { table: string; bounds: GcBounds }): Promise<ApiResult<GcRunResult>> =>
		typedAs<GcRunResult>(await post(`/v1/table/${enc(table)}/maintenance/run`, bounds)),
);

/** #76 compact small fragments on demand (non-destructive — writes a new version). Owner-gated. */
export const compactTable = command(
	v.object({ table: v.string(), targetRowsPerFragment: v.optional(v.nullable(v.number())) }),
	async ({ table, targetRowsPerFragment }): Promise<ApiResult<CompactResult>> =>
		typedAs<CompactResult>(
			await post(`/v1/table/${enc(table)}/maintenance/compact`, {
				target_rows_per_fragment: targetRowsPerFragment ?? null,
			}),
		),
);

// ── version refs: tags, branches, restore ──────────────────────────────────────────────────────

/** #64 name (tag) a Lance version. Writer-gated (can_create_tag). */
export const createTableTag = command(
	v.object({ table: v.string(), tag: v.string(), version: v.number() }),
	async ({ table, tag, version }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/tags/create`, { tag, version }),
);

/** #74 delete a tag (writer-gated). */
export const deleteTableTag = command(
	v.object({ table: v.string(), tag: v.string() }),
	async ({ table, tag }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/tags/delete`, { tag }),
);

/** #74 move a tag to another version (owner-gated can_update_tag). */
export const moveTableTag = command(
	v.object({ table: v.string(), tag: v.string(), version: v.number() }),
	async ({ table, tag, version }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/tags/update`, { tag, version }),
);

/** #74 create a branch from a version (owner-gated can_create_branch). */
export const createTableBranch = command(
	v.object({
		table: v.string(),
		name: v.string(),
		fromVersion: v.optional(v.nullable(v.number())),
	}),
	async ({ table, name, fromVersion }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/branches/create`, { name, from_version: fromVersion ?? null }),
);

/** #74 delete a branch (writer-gated). */
export const deleteTableBranch = command(
	v.object({ table: v.string(), name: v.string() }),
	async ({ table, name }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/branches/delete`, { name }),
);

/** #64 restore the table to a prior version. Restore mints a FRESH version pointing at the restored
 *  data (history is never rewritten); owner-gated (can_restore). */
export const restoreTableVersion = command(
	v.object({ table: v.string(), version: v.number() }),
	async ({ table, version }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/restore`, { version }),
);

// ── schema evolution ───────────────────────────────────────────────────────────────────────────

/** #74 add a SQL-expression column. Writer-gated (can_write_data). */
export const addColumn = command(
	v.object({ table: v.string(), name: v.string(), expression: v.string() }),
	async ({ table, name, expression }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/add_columns`, { new_columns: [{ name, expression }] }),
);

/** #74 rename an existing column (alter_columns path→rename). Writer-gated. */
export const renameColumn = command(
	v.object({ table: v.string(), path: v.string(), rename: v.string() }),
	async ({ table, path, rename }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/alter_columns`, { alterations: [{ path, rename }] }),
);

/** #74 drop a column. Writer-gated. */
export const dropColumn = command(
	v.object({ table: v.string(), name: v.string() }),
	async ({ table, name }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/drop_columns`, { columns: [name] }),
);

/** #74 tail — re-type an existing column (alter_columns path→data_type). A cast Lance can't perform
 *  (e.g. string→int on non-numeric text) 400s at the catalog, surfaced. Writer-gated. */
export const retypeColumn = command(
	v.object({ table: v.string(), path: v.string(), type: v.string() }),
	async ({ table, path, type }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/alter_columns`, {
			alterations: [{ path, data_type: { type } }],
		}),
);

/** #74 tail — merge or replace one field's metadata (update_field_metadata). A `null` value deletes
 *  that key. Writer-gated (can_write_data). */
export const setFieldMetadata = command(
	v.object({
		table: v.string(),
		path: v.string(),
		metadata: v.record(v.string(), v.nullable(v.string())),
		replace: v.optional(v.boolean()),
	}),
	async ({ table, path, metadata, replace }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/update_field_metadata`, {
			updates: [{ path, metadata, replace: replace ?? false }],
		}),
);

/** #74 tail — SET the table's schema-level metadata map (schema_metadata/update replaces the whole
 *  map, so the caller sends the full desired map). Writer-gated (can_write_data). */
export const setTableProperties = command(
	v.object({ table: v.string(), metadata: v.record(v.string(), v.string()) }),
	async ({ table, metadata }): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/schema_metadata/update`, { metadata }),
);

/** #85 backfill values into a column (async native job — the response is a job_id, and the version
 *  bump is reconciled when the job lands). Optional `where` bounds the backfill. Writer-gated. */
export const backfillColumn = command(
	v.object({ table: v.string(), column: v.string(), where: v.optional(v.string()) }),
	async ({ table, column, where }): Promise<ApiResult<BackfillResult>> => {
		const body: { column: string; where?: string } = { column };
		if (where) body.where = where;
		return parsed(
			await post(`/v1/table/${enc(table)}/backfill_column`, body),
			BackfillResponseSchema,
		);
	},
);

// ── indexes ────────────────────────────────────────────────────────────────────────────────────

/** #73 build an index — `scalar` picks the catalog's create_scalar_index (BTREE/BITMAP/INVERTED …)
 *  vs create_index (the IVF/HNSW vector families). A rebuild is just a create with the same column
 *  (Lance replaces the existing one). Writer-gated (can_write_data). */
export const createTableIndex = command(
	v.object({
		table: v.string(),
		body: v.object({
			column: v.string(),
			index_type: v.string(),
			distance_type: v.optional(v.string()),
		}),
		scalar: v.boolean(),
	}),
	async ({
		table,
		body,
		scalar,
	}: {
		table: string;
		body: CreateIndexBody;
		scalar: boolean;
	}): Promise<ApiResult<unknown>> =>
		post(`/v1/table/${enc(table)}/${scalar ? 'create_scalar_index' : 'create_index'}`, body),
);

/** #73 drop a named index — writer-gated (can_write_data). */
export const dropTableIndex = command(
	v.object({ table: v.string(), name: v.string() }),
	async ({ table, name }): Promise<ApiResult<unknown>> =>
		catalogJSON(`/v1/table/${enc(table)}/index/${enc(name)}/drop`, { method: 'POST' }),
);

// ── the table lifecycle (#85) ──────────────────────────────────────────────────────────────────

/** #85 drop the table (deletes data + revokes its grants). Owner-gated by the catalog (can_drop).
 *  The registry list is refreshed in the SAME flight — the id it listed no longer names a table. */
export const dropTable = command(
	TableArg,
	async ({ table }): Promise<ApiResult<TableLifecycleResult>> => {
		const result = parsed(
			await catalogJSON(`/v1/table/${enc(table)}/drop`, { method: 'POST' }),
			TableLifecycleResponseSchema,
		);
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);

/** #85 deregister the table (detach from the catalog, data stays on storage). Owner-gated
 *  (can_deregister). */
export const deregisterTable = command(
	TableArg,
	async ({ table }): Promise<ApiResult<TableLifecycleResult>> => {
		const result = parsed(
			await catalogJSON(`/v1/table/${enc(table)}/deregister`, { method: 'POST' }),
			TableLifecycleResponseSchema,
		);
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);

/** #85 rename the table within its namespace (POSTs {new_table_name}; the catalog's in-process #5b
 *  rename relocates the data + migrates FGA ownership). Gated can_drop on the source AND
 *  can_create_table on the destination parent. */
export const renameTable = command(
	v.object({ table: v.string(), newName: v.string() }),
	async ({ table, newName }): Promise<ApiResult<TableLifecycleResult>> => {
		const result = parsed(
			await post(`/v1/table/${enc(table)}/rename`, { new_table_name: newName }),
			RenameTableResponseSchema,
		);
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);

/** #85 declare an empty table `<namespace>$<name>` — the browser-shaped create (JSON body, no Arrow);
 *  the catalog reserves the id, seeds the caller's ownership, and emits the DECLARE_TABLE marker.
 *  `location` optional (empty → the catalog picks). Gated can_create_table on the parent namespace. */
export const declareTable = command(
	v.object({ namespace: v.string(), name: v.string(), location: v.optional(v.string()) }),
	async ({ namespace, name, location }): Promise<ApiResult<DeclareTableResult>> => {
		const result = parsed(
			await post(`/v1/table/${enc(`${namespace}$${name}`)}/declare`, location ? { location } : {}),
			DeclareTableResponseSchema,
		);
		if (result.ok) void fetchTables().refresh();
		return result;
	},
);

/** #85 update rows matching a SQL predicate. `updates` is the wire's [[column, expression], …] SET
 *  list (required); `predicate` optional (absent → all rows). Writer-gated (can_write_data).
 *  Returns the affected-row count + new version. */
export const updateRows = command(
	v.object({
		table: v.string(),
		predicate: v.nullable(v.string()),
		updates: v.array(v.tuple([v.string(), v.string()])),
	}),
	async ({ table, predicate, updates }): Promise<ApiResult<UpdateRowsResult>> => {
		const body: { predicate?: string; updates: [string, string][] } = { updates };
		if (predicate) body.predicate = predicate;
		return parsed(await post(`/v1/table/${enc(table)}/update`, body), UpdateRowsResponseSchema);
	},
);

/** #85 delete rows matching a SQL predicate (required on the wire — there is no "delete all"
 *  default). Writer-gated (can_write_data). The response carries only the new version — the wire has
 *  no deleted-row count. */
export const deleteRows = command(
	v.object({ table: v.string(), predicate: v.string() }),
	async ({ table, predicate }): Promise<ApiResult<DeleteRowsResult>> =>
		parsed(await post(`/v1/table/${enc(table)}/delete`, { predicate }), DeleteRowsResponseSchema),
);

/** The user-state documents this zone owns — its dock's arrangement and its named views. The
 *  `capi/v1/user-state` proxy died with the transport convergence, so the dock reads and writes
 *  through these two like every other JSON value surface in the zone. */
const UserStateArg = v.object({
	document: v.picklist(['dock-layout', 'dock-layout-library']),
});

/** The catalog's envelope: `{exists, value}` — `exists: false` is a genuine "never saved". */
export interface UserStateEnvelope {
	exists?: boolean;
	value?: unknown;
}

/** Read the caller's own document. The three-outcome mapping (ok / absent / unreadable) stays in
 *  the dock's store, which is where it is tested — this only carries the status faithfully. */
export const readUserStateDoc = query(
	UserStateArg,
	async ({ document }): Promise<ApiResult<UserStateEnvelope>> =>
		typedAs<UserStateEnvelope>(await catalogJSON(`/v1/user-state/${document}`)),
);

/** Save the caller's own document. A refused write is reported as such — never flattened to ok. */
export const writeUserStateDoc = command(
	v.object({ ...UserStateArg.entries, value: v.unknown() }),
	async ({ document, value }): Promise<ApiResult<unknown>> =>
		catalogJSON(`/v1/user-state/${document}`, { method: 'PUT', body: JSON.stringify(value) }),
);
