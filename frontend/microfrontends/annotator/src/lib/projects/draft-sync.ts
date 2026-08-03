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

import { fetchDraft, saveDraft } from './remote/tasks.remote';
import { rowsToShapes } from './draft-shapes.js';

/** Snapshot the table into the task's draft. Returns null on success, or the failure detail —
 *  the caller surfaces it; a silent miss here means work that LOOKS saved but never publishes. */
export async function syncTaskDraft(taskId: string, table: Table): Promise<string | null> {
	const shapes = rowsToShapes(table);
	// `refresh()`, not a bare call: a query instance is cached by its argument, so the SECOND save of
	// a session would guard against the revision the first one started from and 409 forever.
	const draftQuery = fetchDraft({ taskId });
	try {
		await draftQuery.refresh();
	} catch (err) {
		// The zone server itself is unreachable — report it rather than saving a draft with no
		// revision guard, which would clobber whatever is there.
		return `could not read the task draft: ${String(err)}`;
	}
	const current = draftQuery.current;
	const baseRevision =
		current === undefined
			? undefined
			: current.ok
				? current.data.revision
				: current.status === 404
					? null
					: undefined;
	if (baseRevision === undefined)
		return `could not read the task draft: ${current?.ok === false ? current.detail : ''}`;
	const saved = await saveDraft({
		taskId,
		shapes,
		...(baseRevision === null ? {} : { base_revision: baseRevision }),
	});
	return saved.ok ? null : saved.detail;
}
