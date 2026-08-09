/**
 * One item's annotation state, summarized for a GRID cell — the pure half of the bulk surface.
 *
 * The grid renders label STATE per item (status counts, item tags, a transcription excerpt)
 * without mounting a canvas; this projection is what a visible row fetches. Pure over an Arrow
 * table and separately tested, so the grid cannot quietly diverge from what the annotations
 * actually say.
 *
 * Phase 2 adds the ACTIONABLE half of the projection: which rows a ✓-accept would touch
 * (`predictionIds`), which row an inline text edit targets (`textId` — the excerpt's own row,
 * so the edit lands where the reader is looking), and the server-stamped attribution
 * (`reviewer`/`reviewedAt` — the save path writes both on every touched row; the grid only
 * ever RENDERS them, it never claims identity client-side).
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
	/** The row id behind `text` — the target of an inline edit. Null when there is no text row. */
	textId: string | null;
	/** Ids of rows still in `prediction` — the set a ✓-accept flips in one save. */
	predictionIds: string[];
	/** Who last touched a row on this item (server-stamped `reviewer` of the newest edit). */
	reviewer: string;
	/** When that touch happened — epoch ms from the newest `updated_at`; null when unstamped. */
	reviewedAt: number | null;
}

export function summarize(table: Table): AnnotationSummary {
	const id = table.getChild('id');
	const status = table.getChild('status');
	const shape = table.getChild('shape_type');
	const label = table.getChild('label');
	const text = table.getChild('text');
	const reviewer = table.getChild('reviewer');
	const updatedAt = table.getChild('updated_at');
	const byStatus: Record<string, number> = {};
	const tags = new Set<string>();
	const predictionIds: string[] = [];
	let excerpt = '';
	let excerptId: string | null = null;
	let latestReviewer = '';
	let latestAt: number | null = null;
	for (let i = 0; i < table.numRows; i++) {
		const s = String(status?.get(i) ?? '') || 'unlabelled';
		byStatus[s] = (byStatus[s] ?? 0) + 1;
		if (s === 'prediction') predictionIds.push(String(id?.get(i) ?? i));
		if (String(shape?.get(i) ?? '') === 'tag') {
			const tag = String(label?.get(i) ?? '');
			if (tag) tags.add(tag);
		}
		if (!excerpt) {
			const value = String(text?.get(i) ?? '');
			if (value) {
				excerpt = value;
				excerptId = String(id?.get(i) ?? i);
			}
		}
		const who = String(reviewer?.get(i) ?? '');
		if (who) {
			// Arrow timestamps surface as epoch ms; a reviewer without a timestamp still wins
			// over nobody, so the stamp is optional per row.
			const at = updatedAt?.get(i);
			const ms = at == null ? null : Number(at);
			if (latestAt === null || (ms !== null && ms >= latestAt)) {
				latestReviewer = who;
				latestAt = ms ?? latestAt;
			}
		}
	}
	return {
		total: table.numRows,
		byStatus,
		tags: [...tags].sort(),
		text: excerpt,
		textId: excerptId,
		predictionIds,
		reviewer: latestReviewer,
		reviewedAt: latestAt,
	};
}
