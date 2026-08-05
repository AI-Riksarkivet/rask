/**
 * Which actions a SELECTION can perform — the bulk vocabulary, derived.
 *
 * The queue offered exactly two bulk actions, `accept` and `assign`, hand-picked; meanwhile every
 * per-row button came from that task's OWN `legal_events`. So the row menu could claim, submit,
 * skip, requeue, release and reopen, and the selection could not — which is why the surface read as
 * "pick a row, do a thing" no matter how good the selection model was. Claiming twenty items meant
 * twenty clicks.
 *
 * Derived from the same source the row buttons read, so the two can never drift: a new edge in the
 * task machine shows up in both at once, or in neither.
 */

import type { TaskDetail } from './types';

/**
 * Events that are deliberately NOT bulk actions.
 *
 * - `assign` and `request_changes` each carry an ARGUMENT a person must type (a recipient, a note).
 *   They stay dialog-driven; `assign` already has its own bulk dialog, and a single note pasted onto
 *   forty items would be a review that says nothing about any of them.
 * - `save_draft` belongs to the canvas, not to a queue row.
 */
export const NOT_BULK: readonly string[] = ['assign', 'request_changes', 'save_draft'];

/**
 * Display order for the bulk buttons.
 *
 * FIXED rather than derived from the data, because the buttons must not reorder themselves as the
 * selection changes — a control that moves under the cursor between two clicks is how someone
 * accepts a batch they meant to skip. Roughly the order work flows: pick it up, put it down, send
 * it on, judge it, undo.
 */
const ORDER: readonly string[] = [
	'claim',
	'release',
	'submit',
	'skip',
	'accept',
	'fix_and_accept',
	'requeue',
	'reopen',
];

/** One offerable bulk action and how many of the selected rows will actually take it. */
export interface BulkAction {
	event: string;
	/** Rows in the selection whose own `legal_events` include this event. */
	count: number;
}

/**
 * The bulk actions available for a selection, with per-event counts.
 *
 * An event is offered when AT LEAST ONE selected row declares it legal, and the count says how
 * many — so "Claim 12" on a selection of 20 is honest about the other 8 rather than silently
 * acting on a subset or refusing the whole thing. The server still gates every item individually;
 * this only stops the UI offering work it can already see is impossible.
 */
export function bulkActions(
	tasks: readonly TaskDetail[],
	selectedIds: readonly string[],
): BulkAction[] {
	const selected = new Set(selectedIds);
	const counts = new Map<string, number>();

	for (const task of tasks) {
		if (!selected.has(task.task_id)) continue;
		for (const legal of task.legal_events) {
			if (NOT_BULK.includes(legal.event)) continue;
			counts.set(legal.event, (counts.get(legal.event) ?? 0) + 1);
		}
	}

	const known = ORDER.filter((event) => counts.has(event)).map((event) => ({
		event,
		count: counts.get(event)!,
	}));
	// An event the machine grows that nobody added to ORDER still appears, after the known ones and
	// alphabetically. Dropping it would make a new edge silently un-bulk-able, which is the failure
	// this whole function exists to end.
	const extra = [...counts.keys()]
		.filter((event) => !ORDER.includes(event))
		.sort()
		.map((event) => ({ event, count: counts.get(event)! }));

	return [...known, ...extra];
}

/** The rows a given bulk event will actually be fired for. */
export function targetsFor(
	tasks: readonly TaskDetail[],
	selectedIds: readonly string[],
	event: string,
): TaskDetail[] {
	const selected = new Set(selectedIds);
	return tasks.filter(
		(task) => selected.has(task.task_id) && task.legal_events.some((e) => e.event === event),
	);
}
