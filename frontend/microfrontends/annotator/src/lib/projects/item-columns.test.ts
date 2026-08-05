/**
 * The item's own columns.
 *
 * The queue's five columns were all task ADMINISTRATION — key, state, lease, provenance, actions.
 * Nothing described the thing being labelled, so the questions that decide what to work on next
 * ("which of these did the machine call `letter`", "show me the audio ones", "which came from vasa")
 * had no answer in the table.
 *
 * The first test is the load-bearing one: `prediction` was added to the task server-side and never
 * declared in the client's wire schema, so valibot stripped it in silence — the label a bulk send
 * applied was invisible in the very queue you review it from.
 */

import { describe, expect, it } from 'vitest';
import * as v from 'valibot';

import {
	corpusText,
	distinctValues,
	labelText,
	matchesText,
	mediaText,
	predictedLabels,
} from './item-columns';
import { TaskDetailSchema } from './types';
import type { TaskDetail } from './types';

function task(over: Partial<TaskDetail> = {}): TaskDetail {
	return {
		task_id: 't1',
		project_id: 'p1',
		state: 'unassigned',
		assignee: null,
		source: { kind: 'chunks', keys: ['doc/1/1'], where: null },
		media: { kind: 'image' },
		prediction: [],
		review_notes: [],
		legal_events: [],
		...over,
	} as unknown as TaskDetail;
}

describe('the prediction must SURVIVE the wire schema', () => {
	it('decodes a task carrying a prediction', () => {
		// The defect. The server returns the whole task document (list_project_tasks spreads `**doc`),
		// so `prediction` is on the wire — but valibot's `v.object` strips what it does not declare,
		// in silence. Every bulk-applied label was invisible in the queue that reviews it.
		const decoded = v.parse(TaskDetailSchema, {
			task_id: 't1',
			project_id: 'p1',
			state: 'unassigned',
			source: { kind: 'chunks', keys: ['doc/1/1'] },
			media: { kind: 'image' },
			prediction: [{ shape_type: 'tag', label: 'letter', source: 'bulk' }],
		});

		expect(decoded.prediction?.[0]?.label).toBe('letter');
	});

	it('keeps the SERVER-stamped source, so suggested work is distinguishable from drawn work', () => {
		const decoded = v.parse(TaskDetailSchema, {
			task_id: 't1',
			project_id: 'p1',
			state: 'unassigned',
			source: { kind: 'chunks', keys: ['k'] },
			media: { kind: 'image' },
			prediction: [{ shape_type: 'tag', label: 'letter', source: 'bulk' }],
		});

		expect(decoded.prediction?.[0]?.source).toBe('bulk');
	});

	it('defaults to an empty list for a task with no prediction', () => {
		const decoded = v.parse(TaskDetailSchema, {
			task_id: 't1',
			project_id: 'p1',
			state: 'unassigned',
			source: { kind: 'chunks', keys: ['k'] },
			media: { kind: 'image' },
		});

		expect(decoded.prediction).toEqual([]);
	});
});

describe('the label column', () => {
	it('reads the labels off the prediction', () => {
		expect(predictedLabels(task({ prediction: [{ shape_type: 'tag', label: 'letter' }] }))).toEqual([
			'letter',
		]);
	});

	it('shows EVERY distinct label, not just the first', () => {
		// A bulk send attaches one tag, but an import or a model can carry several shapes. Showing
		// only the first silently misreports a multi-label item.
		const t = task({
			prediction: [
				{ shape_type: 'tag', label: 'letter' },
				{ shape_type: 'bbox', label: 'stamp' },
			],
		});

		expect(labelText(t)).toBe('letter, stamp');
	});

	it('de-duplicates a label repeated across shapes', () => {
		const t = task({
			prediction: [
				{ shape_type: 'bbox', label: 'stamp' },
				{ shape_type: 'bbox', label: 'stamp' },
			],
		});

		expect(labelText(t)).toBe('stamp');
	});

	it('drops shapes with no label rather than rendering a blank entry', () => {
		const t = task({
			prediction: [{ shape_type: 'bbox' }, { shape_type: 'tag', label: 'letter' }],
		});

		expect(labelText(t)).toBe('letter');
	});

	it('is a STRING, because the table sorts on a scalar', () => {
		// An array accessor sorts by reference — an order that looks arbitrary and is stable enough
		// that nobody notices it is wrong.
		expect(typeof labelText(task())).toBe('string');
	});
});

describe('the corpus and modality columns', () => {
	it('reads the corpus off source.where', () => {
		expect(corpusText(task({ source: { kind: 'chunks', keys: ['k'], where: 'vasa' } }))).toBe('vasa');
	});

	it('renders the DEFAULT corpus as empty, not as the word "default"', () => {
		// The column filters by substring, so a placeholder would make `default` match items whose
		// corpus is genuinely named that.
		expect(corpusText(task())).toBe('');
	});

	it('reads the modality off media.kind', () => {
		expect(mediaText(task({ media: { kind: 'audio' } }))).toBe('audio');
	});
});

describe('the filter options come from the DATA', () => {
	it('lists each distinct value once, sorted', () => {
		const tasks = [
			task({ prediction: [{ shape_type: 'tag', label: 'letter' }] }),
			task({ prediction: [{ shape_type: 'tag', label: 'envelope' }] }),
			task({ prediction: [{ shape_type: 'tag', label: 'letter' }] }),
		];

		expect(distinctValues(tasks, labelText)).toEqual(['envelope', 'letter']);
	});

	it('SPLITS a multi-label cell so each label is offered separately', () => {
		// Otherwise the dropdown offers "letter, stamp" as one option, which matches nothing when
		// filtering for either.
		const tasks = [
			task({
				prediction: [
					{ shape_type: 'tag', label: 'letter' },
					{ shape_type: 'bbox', label: 'stamp' },
				],
			}),
		];

		expect(distinctValues(tasks, labelText)).toEqual(['letter', 'stamp']);
	});

	it('offers nothing for a column no task fills', () => {
		expect(distinctValues([task()], labelText)).toEqual([]);
	});
});

describe('free-text search across the item columns', () => {
	it('matches the item key', () => {
		expect(matchesText(task(), 'doc/1')).toBe(true);
	});

	it('matches the label', () => {
		expect(matchesText(task({ prediction: [{ shape_type: 'tag', label: 'letter' }] }), 'lett')).toBe(
			true,
		);
	});

	it('matches the corpus', () => {
		expect(matchesText(task({ source: { kind: 'chunks', keys: ['k'], where: 'vasa' } }), 'vas')).toBe(
			true,
		);
	});

	it('is case-insensitive', () => {
		expect(matchesText(task({ prediction: [{ shape_type: 'tag', label: 'Letter' }] }), 'lETT')).toBe(
			true,
		);
	});

	it('an empty query matches everything, rather than nothing', () => {
		// The difference between "no filter" and "filter for the empty string" — getting it backwards
		// empties the table the moment someone clears the box.
		expect(matchesText(task(), '')).toBe(true);
		expect(matchesText(task(), '   ')).toBe(true);
	});

	it('does not match what is not there', () => {
		expect(matchesText(task(), 'nonsense')).toBe(false);
	});
});
