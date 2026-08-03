/**
 * Canvas → task-draft snapshot (the S10 first half): after a task-opened canvas saves its unit,
 * the unit's annotation rows are copied into the task's DRAFT — the document the publish reads —
 * so shapes drawn on the canvas travel into the published table instead of an accepted task
 * landing as an empty sentinel row.
 *
 * The media annotations plane stays the canvas's own store for now (the full S10 cutover deletes
 * that write path last, per the slice plan); this sync is one keyed write per save on top, guarded
 * by the draft revision (a stale snapshot 409s rather than clobbering).
 */
import type { Table } from 'apache-arrow';

import { tasksApi } from './client.js';
import { rowsToShapes } from './draft-shapes.js';

/** Snapshot the table into the task's draft. Returns null on success, or the failure detail —
 *  the caller surfaces it; a silent miss here means work that LOOKS saved but never publishes. */
export async function syncTaskDraft(taskId: string, table: Table): Promise<string | null> {
	const shapes = rowsToShapes(table);
	const current = await tasksApi.getDraft(taskId);
	const baseRevision = current.ok
		? current.data.revision
		: current.status === 404
			? null
			: undefined;
	if (baseRevision === undefined)
		return `could not read the task draft: ${current.ok ? '' : current.detail}`;
	const saved = await tasksApi.saveDraft(taskId, {
		shapes,
		...(baseRevision === null ? {} : { base_revision: baseRevision }),
	});
	return saved.ok ? null : saved.detail;
}
