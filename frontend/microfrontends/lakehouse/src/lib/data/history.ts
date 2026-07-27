// #113 the pure core of the commit log: how a Lance transaction row reads as "what changed", how a
// version joins to the governed run that wrote it ("by whom"), and how two versions compare. Kept out of
// the component so each rule is testable through the DOM without a live catalog — and so the honesty rules
// (never claim 0, never synthesise an actor, never reinterpret a naive timestamp) live in one place.
import type { ProducerInfo, SchemaField } from '@rask/api/lineage';
import type { HistoryRow } from './catalog';

/** The tone of an operation chip. Lance's operation names, mapped onto the palette the reference console
 * uses for the same idea (`SnapshotCompare.vue` — append green, delete red, overwrite amber, merge info,
 * compact/rewrite teal); `unknown` is the honest tone for a transaction we could not read or an operation
 * Lance added after this table was written. */
export type OpTone = 'append' | 'delete' | 'rewrite' | 'schema' | 'index' | 'restore' | 'unknown';

const OP_TONES: Record<string, OpTone> = {
	append: 'append',
	overwrite: 'rewrite',
	update: 'rewrite',
	delete: 'delete',
	merge: 'schema',
	project: 'schema',
	rewrite: 'index',
	compact: 'index',
	createindex: 'index',
	restore: 'restore',
	updateconfig: 'schema',
	datareplacement: 'rewrite',
};

/** Tone for a Lance operation name. Unrecognised (or null) → `unknown`, never a confident colour. */
export function opTone(operation: string | null | undefined): OpTone {
	if (!operation) return 'unknown';
	return OP_TONES[operation.toLowerCase()] ?? 'unknown';
}

/** The row's own columns — everything else on the wire is transaction DETAIL and belongs in "what changed"
 * plus the raw-field expander. */
const ROW_KEYS = new Set(['version', 'timestamp', 'operation']);

/** One piece of the "what changed" cell. `mono` marks values that are the caller's own text (a predicate,
 * an index name) — those are shown verbatim, never paraphrased. */
export type ChangeBit = { text: string; mono?: boolean };

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** How many of a list-valued detail field the wire reported: the catalog collapses fragment/index lists to
 * a count, but an older/newer backend may send the list itself — accept both rather than render `[object]`. */
function count(value: unknown): number | null {
	if (typeof value === 'number' && Number.isFinite(value)) return value;
	if (Array.isArray(value)) return value.length;
	return null;
}

function text(value: unknown): string | null {
	if (typeof value === 'string' && value.trim() !== '') return value;
	return null;
}

/** The names inside a `new_indices` / `removed_indices` detail field, when the backend sends objects rather
 * than a count. */
function indexNames(value: unknown): string[] {
	if (!Array.isArray(value)) return [];
	return value
		.map((entry) =>
			entry && typeof entry === 'object' && 'name' in entry
				? String((entry as { name: unknown }).name)
				: typeof entry === 'string'
					? entry
					: '',
		)
		.filter((name) => name !== '');
}

/** "What changed", derived from the KEYS PRESENT on the row — never from a per-operation switch.
 *
 * Two honesty rules are enforced here and pinned by the e2e:
 *  - a numeric detail of 0 is not a statement. `fields_modified: 0` comes back on an Update that demonstrably
 *    did modify a column (the backend's own integration test refuses to pin it), and `-0 removed` is noise,
 *    so zeros are dropped rather than rendered.
 *  - the delete predicate is shown exactly as the caller wrote it, in mono. It is the single most useful
 *    field in the log; a "Delete happened" row without it is not an answer.
 */
export function summarizeChange(row: HistoryRow): ChangeBit[] {
	const bits: ChangeBit[] = [];
	const push = (text: string, mono = false) => bits.push(mono ? { text, mono } : { text });
	const detail = row as Record<string, unknown>;

	const predicate = text(detail.predicate);
	if (predicate) push(`where ${predicate}`, true);

	const mode = text(detail.update_mode);
	if (mode) push(mode);

	// Restore's target version. The catalog does not send it yet (reported: adding `version` to its detail
	// whitelist would OVERWRITE the row's own version — it needs a distinct key), so this renders as soon as
	// a `restored_from` arrives and stays silent, not wrong, until then.
	const restored = count(detail.restored_from);
	if (restored !== null) push(`→ v${restored}`, true);

	const fields = count(detail.fields_modified);
	if (fields !== null && fields > 0) push(`${plural(fields, 'field')} rewritten`);

	const added = indexNames(detail.new_indices);
	const addedCount = count(detail.new_indices);
	if (added.length > 0) push(`index ${added.join(', ')}`, true);
	else if (addedCount !== null && addedCount > 0) push(plural(addedCount, 'index', 'indices'));
	const droppedIndexes = count(detail.removed_indices);
	if (droppedIndexes !== null && droppedIndexes > 0)
		push(`-${plural(droppedIndexes, 'index', 'indices')}`);

	const groups = count(detail.groups);
	if (groups !== null && groups > 0) push(plural(groups, 'rewrite group'));

	const fragments = count(detail.fragments);
	if (fragments !== null && fragments > 0) push(plural(fragments, 'fragment'));
	const newFragments = count(detail.new_fragments);
	if (newFragments !== null && newFragments > 0) push(`+${plural(newFragments, 'fragment')}`);
	const updatedFragments = count(detail.updated_fragments);
	if (updatedFragments !== null && updatedFragments > 0)
		push(`${plural(updatedFragments, 'fragment')} rewritten`);
	const removed = count(detail.removed_fragment_ids) ?? 0;
	const deleted = count(detail.deleted_fragment_ids) ?? 0;
	if (removed + deleted > 0) push(`-${plural(removed + deleted, 'fragment')}`);

	if (detail.schema_set === true) push('schema set');
	return bits;
}

/** Every transaction field the wire carried for this version, uninterpreted, in wire order — the escape
 * hatch behind the summary, and what keeps the view forward-compatible with fields the backend adds. */
export function rawFields(row: HistoryRow): { key: string; value: string }[] {
	return Object.entries(row as Record<string, unknown>)
		.filter(([key]) => !ROW_KEYS.has(key))
		.map(([key, value]) => ({ key, value: value === null ? 'null' : String(value) }));
}

/** Whether this row is a schema change, for the "schema changes only" filter. `schema_set` covers the create;
 * Lance's `Project` (a column drop) and `Merge` (a column add) are schema changes that carry `schema`, not
 * `new_schema`, and the catalog currently reports `schema_set: false` for both (reported). Treating the two
 * operation names as schema changes keeps the filter honest against today's backend. */
export function isSchemaChange(row: HistoryRow): boolean {
	if (row.schema_set === true) return true;
	const op = (row.operation ?? '').toLowerCase();
	return op === 'project' || op === 'merge';
}

/** The governed run that wrote a version, plus the runs that also claim it (a retry, or a reconcile after
 * the fact). `null` when no run recorded this version at all. */
export type Authorship = {
	/** The recorded actor, trimmed. `null` when absent OR empty — the compaction service records
	 * `author: ""` (an empty string, not null), which is an absence and must read as one. */
	author: string | null;
	/** The CATALOG's name for the write (`insert`/`create_table`/`ingest_events`) — a different vocabulary
	 * from the format's `operation`, so both are shown. */
	operation: string | null;
	runId: string | null;
	eventTime: string | null;
	rowCount: number | null;
	sizeBytes: number | null;
	qualityPassed: boolean | null;
	qualityAssertions: number;
	/** Additional runs that name this same version, newest first — shown in the expander, not the row. */
	others: ProducerInfo[];
};

/** Join a version to its producing run(s). The producers feed is UNORDERED (AGE returns rows in physical
 * order and nothing downstream sorts), so candidates are sorted by `event_time` DESC and the newest wins —
 * `.find`-ing the first match could surface a stale retry as the author. */
export function authorFor(
	version: number,
	producers: readonly ProducerInfo[] | null | undefined,
): Authorship | null {
	const candidates = (producers ?? [])
		.filter((p) => p.dataset_version != null && Number(p.dataset_version) === version)
		.sort((a, b) => (b.event_time ?? '').localeCompare(a.event_time ?? ''));
	const winner = candidates[0];
	if (!winner) return null;
	const author = (winner.author ?? '').trim();
	return {
		author: author === '' ? null : author,
		operation: winner.operation ?? null,
		runId: winner.run_id ?? null,
		eventTime: winner.event_time ?? null,
		rowCount: winner.row_count ?? null,
		sizeBytes: winner.size_bytes ?? null,
		qualityPassed: winner.quality_passed ?? null,
		qualityAssertions: winner.quality_assertions?.length ?? 0,
		others: candidates.slice(1),
	};
}

/** Runs that tried and produced nothing: a FAIL/ABORT carries no `dataset_version`, so it can never join to
 * a version row. They belong beside the log as attempted writes, never interleaved into it. */
export function failedAttempts(
	producers: readonly ProducerInfo[] | null | undefined,
): ProducerInfo[] {
	return (producers ?? [])
		.filter((p) => p.event_type === 'FAIL' || p.event_type === 'ABORT')
		.sort((a, b) => (b.event_time ?? '').localeCompare(a.event_time ?? ''));
}

/** How an actor renders. The lineage `author` is whatever the write recorded: an opaque OIDC `sub` for a
 * governed API write, a service name for the cascade. We resolve OUR OWN sub (from `/v1/me`) to our email
 * and never guess at anybody else's — an unresolved sub is shown truncated, in full in the title. */
export function displayAuthor(
	author: string,
	me: { sub?: string | null; email?: string | null } | null,
): { text: string; title: string; isMe: boolean } {
	// An opaque OIDC subject is ~60 characters of base64 with no break opportunity. Rendered whole it
	// stretches its column until the rest of the log scrolls out of the card — so the cell always shows a
	// short form and the full value lives in the title.
	const short = author.length > 24 ? `${author.slice(0, 12)}…` : author;
	if (me?.sub && author === me.sub) {
		// `/v1/me` can answer with a null email (a token minted without the `email` scope), and the sub is
		// not a name — fall back to the SHORT sub, never the whole thing.
		const label = me.email ?? short;
		return { text: `${label} (you)`, title: `${label} — OIDC subject ${author}`, isMe: true };
	}
	return {
		text: short,
		title: author.length > 24 ? `OIDC subject ${author}` : author,
		isMe: false,
	};
}

/** One row of the schema diff between two versions.
 *
 * Joined by field NAME, not id: the per-version schema we time-travel to comes from the lineage store's
 * standard `SchemaDatasetFacet`, whose fields are `{name, type, description}` — there is no field id to
 * join on (the reference console joins by id, which is why it can call a rename a rename). A rename
 * therefore appears here as a remove plus an add, and the view says so rather than guessing.
 */
export type SchemaDiffRow = {
	name: string;
	status: 'added' | 'removed' | 'retyped' | 'unchanged';
	type: string | null;
	oldType: string | null;
};

const DIFF_ORDER: Record<SchemaDiffRow['status'], number> = {
	added: 0,
	retyped: 1,
	unchanged: 2,
	removed: 3,
};

export function diffSchemas(
	base: readonly SchemaField[] | null | undefined,
	compare: readonly SchemaField[] | null | undefined,
): SchemaDiffRow[] {
	const left = new Map((base ?? []).map((f) => [f.name, f]));
	const right = new Map((compare ?? []).map((f) => [f.name, f]));
	const rows: SchemaDiffRow[] = [];
	for (const name of new Set([...left.keys(), ...right.keys()])) {
		const l = left.get(name);
		const r = right.get(name);
		if (r && !l) rows.push({ name, status: 'added', type: r.type ?? null, oldType: null });
		else if (l && !r) rows.push({ name, status: 'removed', type: null, oldType: l.type ?? null });
		else if (l && r) {
			const retyped = (l.type ?? '') !== (r.type ?? '');
			rows.push({
				name,
				status: retyped ? 'retyped' : 'unchanged',
				type: r.type ?? null,
				oldType: retyped ? (l.type ?? null) : null,
			});
		}
	}
	return rows.sort(
		(a, b) => DIFF_ORDER[a.status] - DIFF_ORDER[b.status] || a.name.localeCompare(b.name),
	);
}

/** A measured delta between two versions. `null` on either side means the writer reported nothing for that
 * version — the tile is then omitted entirely rather than shown as a misleading 0. */
export type Delta = { base: number | null; compare: number | null; delta: number | null };

export function delta(base: number | null, compare: number | null): Delta {
	return { base, compare, delta: base != null && compare != null ? compare - base : null };
}

/** Bytes as a binary-prefixed size; `null`/absent → `—`, never `0 B` for "unknown". */
export function fmtBytes(n: number | null | undefined): string {
	if (n == null) return '—';
	const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
	let v = n;
	let u = 0;
	while (v >= 1024 && u < units.length - 1) {
		v /= 1024;
		u += 1;
	}
	return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`;
}

/** An epoch (manifest timestamps arrive in ms, branch `createAt` in seconds) as one unambiguous UTC
 * string. The `Z` is the point: the only other timestamp in this feature is offset-less, and a reader must
 * be able to tell which is which. */
export function fmtEpoch(value: number | null | undefined, unit: 'ms' | 's'): string {
	if (value == null) return '—';
	const ms = unit === 's' ? value * 1000 : value;
	return `${new Date(ms).toISOString().replace('T', ' ').slice(0, 16)}Z`;
}

/** An ISO-8601 instant that DOES carry an offset (the lineage store's `event_time`) rendered in UTC, so it
 * lines up with `fmtEpoch`. Anything unparseable is returned verbatim rather than as "Invalid Date". */
export function fmtInstant(value: string | null | undefined): string {
	if (!value) return '—';
	const ms = Date.parse(value);
	if (Number.isNaN(ms)) return value;
	return fmtEpoch(ms, 'ms');
}
