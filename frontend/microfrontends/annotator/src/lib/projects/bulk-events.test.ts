/**
 * The bulk vocabulary.
 *
 * The queue's selection model was always good — checkbox column, row ids that survive a refetch,
 * cleared when a filter changes — and it could do exactly two things: accept and assign. Every
 * OTHER action lived on the row, derived from that task's own `legal_events`. So claiming twenty
 * items meant twenty clicks, and the surface read as "pick a row, do a thing" no matter how many
 * rows you had ticked.
 *
 * These pin that the bulk vocabulary is DERIVED from the same source the row buttons read, so the
 * two cannot drift, and that the counts are honest about a mixed selection.
 */

import { describe, expect, it } from 'vitest';

import { bulkActions, NOT_BULK, targetsFor } from './bulk-events';
import type { TaskDetail } from './types';

function task(id: string, state: string, events: string[]): TaskDetail {
	return {
		task_id: id,
		project_id: 'p1',
		state,
		assignee: null,
		source: { kind: 'chunks', keys: [id] },
		media: { kind: 'image' },
		legal_events: events.map((event) => ({ event })),
		review_notes: [],
		replica_of: null,
		submitted_by: null,
		reviewed_by: null,
	} as unknown as TaskDetail;
}

describe('what a selection can do', () => {
	it('derives the actions from the selected rows own legal events', () => {
		// The whole point. `accept` and `assign` were hardcoded; everything the machine could do to a
		// row was unreachable in bulk.
		const tasks = [task('a', 'unassigned', ['claim', 'assign'])];

		expect(bulkActions(tasks, ['a']).map((x) => x.event)).toEqual(['claim']);
	});

	it('COUNTS how many selected rows will take each action', () => {
		// A mixed selection is the normal case. "Claim 12" on a selection of 20 is honest about the
		// other 8; offering a bare "Claim" would either act on a subset silently or refuse everything.
		const tasks = [
			task('a', 'unassigned', ['claim']),
			task('b', 'unassigned', ['claim']),
			task('c', 'in_review', ['accept']),
		];

		expect(bulkActions(tasks, ['a', 'b', 'c'])).toEqual([
			{ event: 'claim', count: 2 },
			{ event: 'accept', count: 1 },
		]);
	});

	it('ignores rows that are not selected', () => {
		const tasks = [task('a', 'unassigned', ['claim']), task('b', 'in_review', ['accept'])];

		expect(bulkActions(tasks, ['a']).map((x) => x.event)).toEqual(['claim']);
	});

	it('offers nothing for an empty selection', () => {
		expect(bulkActions([task('a', 'unassigned', ['claim'])], [])).toEqual([]);
	});
});

describe('actions that are deliberately not bulk', () => {
	it.each(NOT_BULK)('withholds %s', (event) => {
		// `assign` and `request_changes` each carry an argument a person must type — a recipient, a
		// note. One note pasted onto forty items is a review that says nothing about any of them.
		// `save_draft` belongs to the canvas.
		const tasks = [task('a', 'claimed', [event, 'skip'])];

		expect(bulkActions(tasks, ['a']).map((x) => x.event)).toEqual(['skip']);
	});
});

describe('the buttons must not move under the cursor', () => {
	it('orders by a FIXED sequence, not by the data', () => {
		// A control that reorders itself between two clicks is how someone accepts a batch they meant
		// to skip. The order is roughly how work flows: pick it up, put it down, send it on, judge it.
		const tasks = [task('a', 'x', ['accept', 'claim', 'submit', 'skip'])];

		expect(bulkActions(tasks, ['a']).map((x) => x.event)).toEqual([
			'claim',
			'submit',
			'skip',
			'accept',
		]);
	});

	it('is stable when the selection GROWS', () => {
		const tasks = [task('a', 'x', ['accept']), task('b', 'y', ['claim'])];

		const one = bulkActions(tasks, ['a']).map((x) => x.event);
		const both = bulkActions(tasks, ['a', 'b']).map((x) => x.event);

		// `accept` keeps its position relative to the others rather than jumping to the front because
		// it happened to be selected first.
		expect(one).toEqual(['accept']);
		expect(both).toEqual(['claim', 'accept']);
	});

	it('still surfaces an event nobody added to the order, rather than dropping it', () => {
		// A new edge in the task machine must not become silently un-bulk-able — that is the exact
		// failure this module exists to end.
		const tasks = [task('a', 'x', ['teleport', 'claim'])];

		expect(bulkActions(tasks, ['a']).map((x) => x.event)).toEqual(['claim', 'teleport']);
	});
});

describe('which rows an action actually fires for', () => {
	it('returns only the selected rows that declare the event legal', () => {
		const tasks = [
			task('a', 'unassigned', ['claim']),
			task('b', 'in_review', ['accept']),
			task('c', 'unassigned', ['claim']),
		];

		expect(targetsFor(tasks, ['a', 'b', 'c'], 'claim').map((t) => t.task_id)).toEqual(['a', 'c']);
	});

	it('matches the count the button showed', () => {
		// Otherwise the button says 12 and the loop runs a different number, and the completion notice
		// reports work that never happened.
		const tasks = [
			task('a', 'unassigned', ['claim']),
			task('b', 'unassigned', ['claim']),
			task('c', 'in_review', ['accept']),
		];
		const ids = ['a', 'b', 'c'];

		for (const action of bulkActions(tasks, ids)) {
			expect(targetsFor(tasks, ids, action.event)).toHaveLength(action.count);
		}
	});
});
