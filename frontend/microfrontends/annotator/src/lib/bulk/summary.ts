/**
 * One item's annotation state, summarized for a GRID cell — the pure half of the bulk surface.
 *
 * The grid renders label STATE per item (status counts, item tags, a transcription excerpt)
 * without mounting a canvas; this projection is what a visible row fetches. Pure over an Arrow
 * table and separately tested, so the grid cannot quietly diverge from what the annotations
 * actually say.
 */

import type { Table } from 'apache-arrow';

export interface AnnotationSummary {
	total: number;
	/** Row counts by status; rows with no status count as `unlabelled`. */
	byStatus: Record<string, number>;
	/** Whole-item tags present (the classification chips), sorted. */
	tags: string[];
	/** The first non-empty transcription on the item — the at-a-glance textual content. */
	text: string;
}

export function summarize(table: Table): AnnotationSummary {
	const status = table.getChild('status');
	const shape = table.getChild('shape_type');
	const label = table.getChild('label');
	const text = table.getChild('text');
	const byStatus: Record<string, number> = {};
	const tags = new Set<string>();
	let excerpt = '';
	for (let i = 0; i < table.numRows; i++) {
		const s = String(status?.get(i) ?? '') || 'unlabelled';
		byStatus[s] = (byStatus[s] ?? 0) + 1;
		if (String(shape?.get(i) ?? '') === 'tag') {
			const tag = String(label?.get(i) ?? '');
			if (tag) tags.add(tag);
		}
		if (!excerpt) excerpt = String(text?.get(i) ?? '');
	}
	return { total: table.numRows, byStatus, tags: [...tags].sort(), text: excerpt };
}
