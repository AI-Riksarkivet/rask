/**
 * Row projection — pure functions from (Arrow table, field-override map) to the flat
 * rows the review UI renders. No Svelte, no state: the controller's `$derived` calls
 * these; anything else (a future table view, a test) can too.
 */
import type { Table } from 'apache-arrow';

/** One annotation row projected into the flat shape the sidebar/list render.
 *  Identity is the engine's ROW INDEX (the engine is index-based); `id` is just
 *  a display field. Overlay edits win over the server table. */
export interface AnnoRow {
	index: number;
	id: string;
	label: string;
	status: string;
	shape: string;
	group: string;
	text: string;
	source: string;
	confidence: number | null;
	uncertainty: number | null;
	tStart: number | null;
	tEnd: number | null;
}

/** The raw table cell as a string (null when absent). */
export function rawField(t: Table, field: string, i: number): string | null {
	const v = t.getChild(field)?.get(i);
	return v == null ? null : String(v);
}

/** The overlay-aware cell: a pending edit wins over the server value. Overrides are
 *  keyed `${index}:${field}` (the controller's overlay map). */
export function effectiveField(
	t: Table,
	overrides: ReadonlyMap<string, string>,
	field: string,
	i: number,
): string | null {
	const o = overrides.get(`${i}:${field}`);
	if (o != null) return o;
	return rawField(t, field, i);
}

/** The raw numeric cell (null when absent or non-numeric). */
export function numField(t: Table, field: string, i: number): number | null {
	const v = t.getChild(field)?.get(i);
	return typeof v === 'number' ? v : null;
}

/** Project the whole table to AnnoRows, overlay-aware, with pending deletes filtered
 *  (the sidebar reflects a delete immediately; the canvas reconciles on save+reload). */
export function projectRows(
	t: Table,
	overrides: ReadonlyMap<string, string>,
	deletes: readonly string[],
): AnnoRow[] {
	const out: AnnoRow[] = [];
	for (let i = 0; i < t.numRows; i++) {
		out.push({
			index: i,
			id: rawField(t, 'id', i) ?? String(i),
			label: effectiveField(t, overrides, 'label', i) ?? '',
			status: effectiveField(t, overrides, 'status', i) ?? '',
			shape: effectiveField(t, overrides, 'shape_type', i) ?? '',
			group: effectiveField(t, overrides, 'group', i) ?? '',
			text: effectiveField(t, overrides, 'text', i) ?? '',
			source: effectiveField(t, overrides, 'source', i) ?? '',
			confidence: numField(t, 'confidence', i),
			uncertainty: numField(t, 'uncertainty', i),
			tStart: numField(t, 't_start', i),
			tEnd: numField(t, 't_end', i),
		});
	}
	return deletes.length ? out.filter((r) => !deletes.includes(r.id)) : out;
}
