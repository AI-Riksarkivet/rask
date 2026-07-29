/**
 * Export the workflow's result hits to a downloadable file (client-side only —
 * the hits already live in the browser, so no backend round-trip). An Export
 * node picks the format (JSON / CSV) and which columns to include; both formats
 * honour that column selection.
 *
 * The exportable column set is DERIVED from the active dataset descriptor (its
 * identity keys, time span, display body/caption, and declared metadata fields
 * — plus the rank score and tags), so a flat export names no corpus column.
 */
import { activeView, type Hit } from '@rask/media-api';

/** The exportable columns for the ACTIVE dataset, in canonical export order:
 *  identity keys, time span (+ derived duration), the display body & caption,
 *  each declared metadata field, then the rank score and tags. The per-word
 *  `alignments` arrays are intentionally excluded — a flat export wants scalar
 *  fields. Reads the descriptor, so it MUST be called after it has loaded
 *  (render / export time), never at module-eval. */
export function exportColumns(): string[] {
	const view = activeView();
	const { declared } = view.descriptor;
	const cols: string[] = [...view.keyFields];
	if (declared.time) cols.push(declared.time.start, declared.time.end, 'duration');
	if (view.bodyField) cols.push(view.bodyField);
	if (view.captionField) cols.push(view.captionField);
	for (const { field } of view.metadataFields) cols.push(field);
	cols.push('_score', 'tags');
	return [...new Set(cols)]; // dedupe (a metadata field may repeat a key/body)
}

/** Keep only columns valid for the active dataset, in canonical export order —
 *  defends against stale persisted selections and guarantees a stable column
 *  order regardless of the order the user toggled them in. */
function orderColumns(columns: readonly string[]): string[] {
	const want = new Set(columns);
	return exportColumns().filter((c) => want.has(c));
}

/** Resolve a stored selection to a concrete ordered list: `null` means "every
 *  column the active dataset offers", an array is filtered + ordered. */
function resolveColumns(columns: readonly string[] | null): string[] {
	return columns === null ? exportColumns() : orderColumns(columns);
}

function csvCell(value: unknown): string {
	if (value == null) return '';
	// Arrays (e.g. `tags`) flatten to a `; `-joined cell so commas don't split it.
	const s = Array.isArray(value) ? value.join('; ') : String(value);
	// Quote + escape only when needed (comma, quote, or newline).
	return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function hitsToJson(hits: readonly Hit[], columns: readonly string[] | null = null): string {
	const cols = resolveColumns(columns);
	const rows = hits.map((h) => {
		const rec = h as Record<string, unknown>;
		const out: Record<string, unknown> = {};
		for (const c of cols) out[c] = rec[c] ?? null;
		return out;
	});
	return JSON.stringify(rows, null, 2);
}

function hitsToCsv(hits: readonly Hit[], columns: readonly string[] | null = null): string {
	const cols = resolveColumns(columns);
	const header = cols.join(',');
	const rows = hits.map((h) =>
		cols.map((c) => csvCell((h as Record<string, unknown>)[c])).join(','),
	);
	return [header, ...rows].join('\n');
}

/** Trigger a browser download of `content` as `filename`. */
function downloadFile(filename: string, content: string, mime: string): void {
	const url = URL.createObjectURL(new Blob([content], { type: mime }));
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	a.click();
	URL.revokeObjectURL(url);
}

/** Download the result hits as JSON or CSV with a timestamped filename, limited
 *  to the chosen columns (`null` = every column the active dataset offers). */
export function exportHits(
	hits: readonly Hit[],
	format: 'json' | 'csv',
	now: Date,
	columns: readonly string[] | null = null,
): void {
	if (hits.length === 0) return;
	const stamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
	if (format === 'json') {
		downloadFile(
			`lance-media-results-${stamp}.json`,
			hitsToJson(hits, columns),
			'application/json',
		);
	} else {
		downloadFile(
			`lance-media-results-${stamp}.csv`,
			hitsToCsv(hits, columns),
			'text/csv;charset=utf-8',
		);
	}
}
