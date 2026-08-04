/**
 * Per-annotator throughput and accept-rate, DERIVED from the task list the queue already reads.
 *
 * No new state, and that constraint is the whole design: a metric stored separately from the facts
 * it summarises is a metric that can disagree with them, and the disagreement is silent. Everything
 * here is a projection of `submitted_by`, `reviewed_by`, `review_action` and `assignee` — the same
 * fields the queue renders — so a number that looks wrong is always traceable to a row you can see.
 *
 * Pure, and separate from any component, so "do the numbers reconcile" is answerable without a
 * browser.
 */

import type { TaskDetail } from './types.js';

export interface AnnotatorRow {
	/** The OIDC subject / username, exactly as the task recorded it. */
	who: string;
	/** Items this person has submitted for review (or straight to accepted on a no-review project). */
	submitted: number;
	/** Of those, how many were accepted. */
	accepted: number;
	/** Of those, how many came back with changes requested. */
	changesRequested: number;
	/** Items currently assigned to them and not yet submitted — the queue they are sitting on. */
	open: number;
	/** `accepted / submitted`, or null when they have submitted nothing.
	 *
	 *  Null rather than 0: a person with nothing submitted has no rate, and rendering 0% would read
	 *  as "everything they did was rejected" — the opposite of the truth. */
	acceptRate: number | null;
}

/** States a task can be in while still sitting with its assignee. */
const OPEN_STATES = new Set(['unassigned', 'claimed', 'changes_requested']);

/**
 * One row per person who appears anywhere on these tasks.
 *
 * Includes people with ZERO submissions on purpose. Someone holding four items and having submitted
 * none is exactly who a manager is looking for, and dropping them because a count is zero would make
 * absence mean two different things — "not involved" and "involved, stuck" — with no way to tell.
 */
export function annotatorMetrics(tasks: TaskDetail[]): AnnotatorRow[] {
	const rows = new Map<string, AnnotatorRow>();
	const row = (who: string): AnnotatorRow => {
		const existing = rows.get(who);
		if (existing) return existing;
		const fresh: AnnotatorRow = {
			who,
			submitted: 0,
			accepted: 0,
			changesRequested: 0,
			open: 0,
			acceptRate: null,
		};
		rows.set(who, fresh);
		return fresh;
	};

	for (const task of tasks) {
		// Whoever SUBMITTED it owns the submission, not whoever currently holds it: a task that came
		// back for changes and was reassigned would otherwise credit the wrong person.
		if (task.submitted_by) {
			const r = row(task.submitted_by);
			r.submitted += 1;
			if (task.state === 'accepted') r.accepted += 1;
			else if (task.review_action === 'request_changes' || task.state === 'changes_requested') {
				r.changesRequested += 1;
			}
		}
		// The queue they are sitting on. A submitted item is not open even if still assigned to them.
		if (task.assignee && !task.submitted_by && OPEN_STATES.has(task.state)) {
			row(task.assignee).open += 1;
		}
		// Named so a reviewer with no submissions of their own still appears — otherwise the panel
		// silently omits the people doing the reviewing.
		if (task.reviewed_by) row(task.reviewed_by);
	}

	for (const r of rows.values()) {
		r.acceptRate = r.submitted === 0 ? null : r.accepted / r.submitted;
	}

	// Busiest first — the ordering a manager reads top-down. Ties by name so the panel does not
	// reshuffle between refetches of identical data.
	return [...rows.values()].sort(
		(a, b) => b.submitted - a.submitted || b.open - a.open || a.who.localeCompare(b.who),
	);
}
