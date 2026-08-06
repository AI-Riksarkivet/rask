/**
 * The item's OWN columns in the task queue — as opposed to the queue's admin columns.
 *
 * The queue showed five columns and every one of them was task administration: the item key, the
 * state, the lease, who submitted and reviewed it, and the actions. Nothing about the THING being
 * labelled. So you could sort by state and filter by assignee, and you could not answer "which of
 * these did the machine call `letter`", "which came from the vasa corpus", or "show me the audio
 * ones" — the questions that actually decide what to work on next.
 *
 * These derive from what the task ALREADY carries, which is the honest scope: `source.where` is the
 * corpus, `media.kind` is the modality, and `prediction` is the pre-annotation a bulk send attached.
 * Columns drawn from the corpus ROW (a date, a page number, the transcribed text) need those rows
 * joined in and are deliberately not faked here — see the note at the bottom.
 */

import type { TaskDetail } from './types';

/** The labels a task's pre-annotations carry, de-duplicated, in order of appearance.
 *
 *  A bulk send attaches one whole-item `tag`, so this is usually one label — but an imported or
 *  model-produced prediction can carry several shapes, and showing only the first would silently
 *  misreport a multi-label item. */
export function predictedLabels(task: TaskDetail): string[] {
	const labels = (task.prediction ?? [])
		.map((shape) => shape.label)
		.filter((label): label is string => typeof label === 'string' && label.length > 0);
	return [...new Set(labels)];
}

/** One sortable/filterable string for the label column.
 *
 *  Joined rather than left as an array because TanStack sorts and filters on a scalar; an array
 *  accessor sorts by reference and produces an order that looks random and is stable enough that
 *  nobody notices it is wrong. */
export function labelText(task: TaskDetail): string {
	return predictedLabels(task).join(', ');
}

/** The corpus an item came from — `source.where`, or the empty string for the default.
 *
 *  Empty rather than a placeholder like "default": the column is filtered by substring, and a
 *  placeholder would make `default` match items whose corpus is genuinely named something else. */
export function corpusText(task: TaskDetail): string {
	return task.source.where ?? '';
}

/** The source dataset's VERSION at send time — the reproducibility pin (#83).
 *
 *  Kept out of `corpusText` deliberately, even though they render together: that string is what the
 *  corpus column FILTERS on, and folding a version into it would make a search for `2` match every
 *  item whose corpus happens to be at v2. A pin is provenance, not a search key. */
export function datasetVersionText(task: TaskDetail): string {
	return task.source.dataset_version != null ? `v${task.source.dataset_version}` : '';
}

/** The modality. Already on every task (`media.kind`) and never shown. */
export function mediaText(task: TaskDetail): string {
	return task.media.kind ?? '';
}

/** Every distinct value a column takes across the given tasks, sorted — the options a filter offers.
 *
 *  Derived from the DATA rather than a hardcoded list, so a corpus with an unexpected modality or a
 *  taxonomy nobody enumerated here still filters. Blank values are dropped: "filter by nothing" is
 *  what clearing the filter already means. */
export function distinctValues(
	tasks: readonly TaskDetail[],
	of: (task: TaskDetail) => string,
): string[] {
	const seen = new Set<string>();
	for (const task of tasks) {
		for (const part of of(task).split(',')) {
			const value = part.trim();
			if (value) seen.add(value);
		}
	}
	return [...seen].sort();
}

/** Does this task match a free-text query across its item columns?
 *
 *  Case-insensitive substring over the key, corpus, label and modality together, because a person
 *  typing into one box means "find this anywhere" — asking them which column it lives in is asking
 *  them to know the schema. */
export function matchesText(task: TaskDetail, query: string): boolean {
	const q = query.trim().toLowerCase();
	if (!q) return true;
	return [task.source.keys.join(' '), corpusText(task), labelText(task), mediaText(task)]
		.join(' ')
		.toLowerCase()
		.includes(q);
}

// NOT DONE, and deliberately not faked: columns drawn from the corpus ROW itself — a date, a page
// number, the transcribed text — which is what "filter to 1890s" needs. A task carries its row KEYS,
// not the row, so those columns require joining the rows in from the viewer for the visible page (or
// capturing a display snapshot at send). That is a real piece of work with a transport decision in
// it, and inventing a column here that silently shows nothing would be worse than its absence.
