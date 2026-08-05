/**
 * Selecting everything a filter matched, not just the page you can see.
 *
 * The header checkbox is TanStack's `toggleAllPageRowsSelected` — it takes the visible page and
 * nothing else. With a page size of 20, filtering a project down to 347 items and wanting to act on
 * all of them meant ticking 20, acting, paging, ticking 20 again, eighteen times. The filter did the
 * hard part and then the selection threw the answer away.
 *
 * This is Label Studio's "Select all N tasks" banner: once the page is fully ticked, offer the rest.
 * Deliberately an OFFER rather than automatic — silently extending a selection past what someone can
 * see is how a bulk action lands on rows nobody looked at.
 */

import type { RowSelectionState } from '@rask/ui/data-table';

import type { TaskDetail } from './types';

/** What the banner above the table should say, if anything. */
export type SelectAllPrompt =
	/** Nothing to offer: the page is not fully selected, or the filter fits on one page. */
	| { kind: 'none' }
	/** The page is fully selected and MORE rows match — offer them. */
	| { kind: 'offer'; pageCount: number; matchingCount: number }
	/** Every matching row is already selected — offer to drop back. */
	| { kind: 'all'; matchingCount: number };

/**
 * Decide the banner from three counts.
 *
 * `matchingCount` is every row the FILTER matched, across pages — not the page, and not the
 * project. Comparing against the project total would offer to select rows the filter excluded,
 * which is the opposite of what someone filtering wants.
 */
export function selectAllPrompt(opts: {
	pageCount: number;
	pageSelectedCount: number;
	selectedCount: number;
	matchingCount: number;
}): SelectAllPrompt {
	const { pageCount, pageSelectedCount, selectedCount, matchingCount } = opts;

	// Nothing selected, or a partially-ticked page: the person is still choosing. Offering "select
	// all 347" mid-choice is noise at best and a misclick at worst.
	if (pageCount === 0 || pageSelectedCount < pageCount) return { kind: 'none' };

	// Everything the filter matched is already held — say so, and offer the way back. Without this
	// the banner would keep offering a selection that is already complete, which reads as the button
	// not having worked.
	//
	// This branch also covers "the filter fits on one page": a full page IS the whole match then, so
	// `selectedCount >= matchingCount` holds and the inner check collapses it to `none`. A separate
	// `matchingCount <= pageCount` guard below would be UNREACHABLE — with the page fully selected,
	// `selectedCount >= pageCount >= matchingCount` always. It was written, and a mutation that
	// deleted it changed nothing, which is how it was found.
	if (selectedCount >= matchingCount) {
		return matchingCount > pageCount ? { kind: 'all', matchingCount } : { kind: 'none' };
	}

	return { kind: 'offer', pageCount, matchingCount };
}

/**
 * A selection holding every matching row.
 *
 * Keyed by `task_id`, which is what `getRowId` gives TanStack — so this is the same shape the table
 * writes and the grid reads, and a row selected here stays selected when it scrolls onto another
 * page.
 */
export function selectionOf(tasks: readonly TaskDetail[]): RowSelectionState {
	const next: RowSelectionState = {};
	for (const task of tasks) next[task.task_id] = true;
	return next;
}
