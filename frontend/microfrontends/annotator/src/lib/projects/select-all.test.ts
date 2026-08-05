/**
 * Selecting everything a filter matched.
 *
 * The header checkbox takes the visible page and nothing else, so filtering to 347 items and wanting
 * to act on all of them meant eighteen rounds of tick-twenty-and-act. The filter did the hard part
 * and the selection threw the answer away.
 *
 * The rules that matter here are about WHEN to offer. Extending a selection past what someone can
 * see, without being asked, is how a bulk action lands on rows nobody looked at.
 */

import { describe, expect, it } from 'vitest';

import { selectAllPrompt, selectionOf } from './select-all';
import type { TaskDetail } from './types';

const task = (id: string) => ({ task_id: id }) as TaskDetail;

describe('when to offer the rest of the filter', () => {
	it('offers once the whole PAGE is selected and more rows match', () => {
		const prompt = selectAllPrompt({
			pageCount: 20,
			pageSelectedCount: 20,
			selectedCount: 20,
			matchingCount: 347,
		});

		expect(prompt).toEqual({ kind: 'offer', pageCount: 20, matchingCount: 347 });
	});

	it('stays SILENT while the page is only partly selected', () => {
		// The person is still choosing. Offering "select all 347" mid-choice is noise at best and a
		// misclick at worst.
		const prompt = selectAllPrompt({
			pageCount: 20,
			pageSelectedCount: 7,
			selectedCount: 7,
			matchingCount: 347,
		});

		expect(prompt.kind).toBe('none');
	});

	it('stays silent when nothing is selected', () => {
		expect(
			selectAllPrompt({ pageCount: 20, pageSelectedCount: 0, selectedCount: 0, matchingCount: 347 })
				.kind,
		).toBe('none');
	});

	it('stays silent when the filter FITS on one page', () => {
		// There is no "rest" to offer, and a banner saying "select all 12" above 12 already-selected
		// rows is furniture.
		expect(
			selectAllPrompt({ pageCount: 20, pageSelectedCount: 12, selectedCount: 12, matchingCount: 12 })
				.kind,
		).toBe('none');
	});

	it('stays silent on an empty table', () => {
		expect(
			selectAllPrompt({ pageCount: 0, pageSelectedCount: 0, selectedCount: 0, matchingCount: 0 })
				.kind,
		).toBe('none');
	});
});

describe('once everything matching is held', () => {
	it('says so, and offers the way back', () => {
		// Without this the banner keeps offering a selection that is already complete — which reads as
		// the button not having worked.
		const prompt = selectAllPrompt({
			pageCount: 20,
			pageSelectedCount: 20,
			selectedCount: 347,
			matchingCount: 347,
		});

		expect(prompt).toEqual({ kind: 'all', matchingCount: 347 });
	});

	it('does not claim "all" when the filter fit on one page anyway', () => {
		expect(
			selectAllPrompt({ pageCount: 20, pageSelectedCount: 20, selectedCount: 20, matchingCount: 20 })
				.kind,
		).toBe('none');
	});

	it('treats a selection LARGER than the match as complete', () => {
		// Rows selected under a previous filter can outnumber the current match. The banner must not
		// then offer to "select all 5" while 40 are held — it is already past complete.
		const prompt = selectAllPrompt({
			pageCount: 3,
			pageSelectedCount: 3,
			selectedCount: 40,
			matchingCount: 5,
		});

		expect(prompt.kind).toBe('all');
	});
});

describe('the selection it produces', () => {
	it('is keyed by task id, like the table writes it', () => {
		// `getRowId` is the task id, so a row selected here stays selected when it scrolls onto
		// another page — and the grid, which reads the same object, agrees.
		expect(selectionOf([task('a'), task('b')])).toEqual({ a: true, b: true });
	});

	it('is empty for no tasks, not undefined', () => {
		expect(selectionOf([])).toEqual({});
	});

	it('holds every task, not just the first page worth', () => {
		const many = Array.from({ length: 347 }, (_, i) => task(`t${i}`));

		expect(Object.keys(selectionOf(many))).toHaveLength(347);
	});
});
