// #113 the pure core of the commit log: how a Lance transaction row reads as "what changed". Kept out of
// the component so each rule is testable through the DOM without a live catalog — and so the honesty rules
// (never claim 0, never reinterpret a naive timestamp) live in one place.
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
