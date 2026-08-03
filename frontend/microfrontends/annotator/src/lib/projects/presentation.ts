/**
 * Pure presentation maps for the projects plane — kept out of components so the chip colours and
 * progress math are unit-testable and cannot fork per surface.
 */
import type { BadgeVariant } from '@rask/ui/badge';

import type { ProjectState, TaskState } from './types.js';

/** State → badge variant. `publish_failed` is destructive on purpose: it is an operator condition
 *  with a retry action, not a neutral fact. */
export function projectStateVariant(state: ProjectState): BadgeVariant {
	switch (state) {
		case 'draft':
			return 'secondary';
		case 'labeling':
			return 'default';
		case 'frozen':
			return 'outline';
		case 'publishing':
			return 'warning';
		case 'published':
			return 'success';
		case 'publish_failed':
			return 'destructive';
		case 'archived':
			return 'secondary';
	}
}

export function taskStateVariant(state: TaskState): BadgeVariant {
	switch (state) {
		case 'unassigned':
			return 'secondary';
		case 'claimed':
			return 'default';
		case 'in_review':
			return 'warning';
		case 'changes_requested':
			return 'destructive';
		case 'accepted':
			return 'success';
		case 'skipped':
			return 'outline';
	}
}

/** Terminal-vs-total off the project's derived counts — the landing card's progress bar. */
export function taskProgress(counts: Record<string, number>): { done: number; total: number } {
	const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
	const done = (counts['accepted'] ?? 0) + (counts['skipped'] ?? 0);
	return { done, total };
}

/** The human word for each event button. The EVENT names stay the machine's — this is labelling,
 *  not a second vocabulary: `fix_and_accept` must remain distinguishable from `accept`. */
export const EVENT_LABELS: Record<string, string> = {
	open: 'Open for labeling',
	freeze: 'Freeze',
	publish: 'Publish…',
	archive: 'Archive',
	send: 'Send items…',
	claim: 'Claim',
	assign: 'Assign…',
	save_draft: 'Save draft',
	submit: 'Submit for review',
	release: 'Release',
	skip: 'Skip',
	requeue: 'Requeue',
	accept: 'Accept',
	fix_and_accept: 'Fix & accept',
	request_changes: 'Request changes…',
	reopen: 'Reopen',
};
