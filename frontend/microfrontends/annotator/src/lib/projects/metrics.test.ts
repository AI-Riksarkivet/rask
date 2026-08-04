/**
 * Per-annotator metrics — and specifically, that they cannot disagree with the queue.
 *
 * The constraint is the design: everything is derived from the same fields the queue renders, so a
 * number that looks wrong is always traceable to a row you can see. A metric stored beside the facts
 * it summarises can drift from them, and the drift is silent.
 */

import { describe, expect, it } from 'vitest';

import { annotatorMetrics } from './metrics.js';
import type { TaskDetail } from './types.js';

function task(over: Partial<TaskDetail> = {}): TaskDetail {
	return {
		task_id: 't',
		project_id: 'p1',
		state: 'unassigned',
		assignee: null,
		lease_expires_at: null,
		source: { kind: 'chunks', keys: ['d/0/1'], where: null },
		media: { kind: 'image', image_url: null, media_url: null },
		review_required: true,
		replica_of: null,
		submitted_by: null,
		submitted_at: null,
		reviewed_by: null,
		reviewed_at: null,
		review_action: null,
		review_notes: [],
		legal_events: [],
		...over,
	} as TaskDetail;
}

const by = (rows: ReturnType<typeof annotatorMetrics>, who: string) =>
	rows.find((r) => r.who === who);

describe('annotatorMetrics', () => {
	it('counts submissions and accepts, and the rate is their ratio', () => {
		const rows = annotatorMetrics([
			task({ submitted_by: 'gina', state: 'accepted' }),
			task({ submitted_by: 'gina', state: 'accepted' }),
			task({ submitted_by: 'gina', state: 'in_review' }),
			task({ submitted_by: 'gina', state: 'changes_requested', review_action: 'request_changes' }),
		]);

		const gina = by(rows, 'gina')!;
		expect(gina.submitted).toBe(4);
		expect(gina.accepted).toBe(2);
		expect(gina.changesRequested).toBe(1);
		expect(gina.acceptRate).toBe(0.5);
	});

	it('credits the person who SUBMITTED, not whoever holds it now', () => {
		// A task that came back for changes and was reassigned would otherwise credit the wrong
		// person — and the wrong person is the one who has to explain the number.
		const rows = annotatorMetrics([
			task({ submitted_by: 'gina', assignee: 'omar', state: 'changes_requested' }),
		]);

		expect(by(rows, 'gina')!.submitted).toBe(1);
		expect(by(rows, 'omar')?.submitted ?? 0).toBe(0);
	});

	it('shows a person with ZERO submissions rather than omitting them', () => {
		// Someone holding four items and having submitted none is exactly who a manager is looking
		// for. Dropping them would make absence mean two different things — "not involved" and
		// "involved, stuck" — with no way to tell which.
		const rows = annotatorMetrics([
			task({ assignee: 'omar', state: 'claimed' }),
			task({ assignee: 'omar', state: 'claimed' }),
		]);

		const omar = by(rows, 'omar');
		expect(omar, 'a person with no submissions vanished from the panel').toBeDefined();
		expect(omar!.submitted).toBe(0);
		expect(omar!.open).toBe(2);
	});

	it('a person with nothing submitted has NO rate, not a rate of zero', () => {
		// 0% reads as "everything they did was rejected" — the opposite of the truth.
		const rows = annotatorMetrics([task({ assignee: 'omar', state: 'claimed' })]);
		expect(by(rows, 'omar')!.acceptRate).toBeNull();
	});

	it('a submitted item is not OPEN, even while still assigned to them', () => {
		const rows = annotatorMetrics([
			task({ assignee: 'gina', submitted_by: 'gina', state: 'in_review' }),
		]);

		const gina = by(rows, 'gina')!;
		expect(gina.submitted).toBe(1);
		expect(gina.open, 'a submitted item was double-counted as still open').toBe(0);
	});

	it('a REVIEWER with no submissions of their own still appears', () => {
		// Otherwise the panel silently omits the people doing the reviewing.
		const rows = annotatorMetrics([
			task({ submitted_by: 'gina', reviewed_by: 'maria', state: 'accepted' }),
		]);

		expect(by(rows, 'maria'), 'the reviewer is invisible in the metrics').toBeDefined();
		expect(by(rows, 'maria')!.submitted).toBe(0);
	});

	it('RECONCILES: every submission is attributed to exactly one person', () => {
		// The bar `open_anno.md` set. If the totals do not add up against the raw rows, the panel is
		// telling a manager something the queue would contradict.
		const tasks = [
			task({ submitted_by: 'gina', state: 'accepted' }),
			task({ submitted_by: 'omar', state: 'accepted' }),
			task({ submitted_by: 'omar', state: 'in_review' }),
			task({ assignee: 'maria', state: 'claimed' }),
			task({ state: 'unassigned' }),
		];
		const rows = annotatorMetrics(tasks);

		const submittedTotal = rows.reduce((n, r) => n + r.submitted, 0);
		const acceptedTotal = rows.reduce((n, r) => n + r.accepted, 0);
		const openTotal = rows.reduce((n, r) => n + r.open, 0);

		expect(submittedTotal).toBe(tasks.filter((t) => t.submitted_by).length);
		expect(acceptedTotal).toBe(
			tasks.filter((t) => t.submitted_by && t.state === 'accepted').length,
		);
		expect(openTotal).toBe(tasks.filter((t) => t.assignee && !t.submitted_by).length);
	});

	it('orders busiest first, and stably', () => {
		// A panel that reshuffles between refetches of identical data reads as live churn.
		const rows = annotatorMetrics([
			task({ submitted_by: 'zoe', state: 'accepted' }),
			task({ submitted_by: 'adam', state: 'accepted' }),
			task({ submitted_by: 'adam', state: 'accepted' }),
		]);

		expect(rows.map((r) => r.who)).toEqual(['adam', 'zoe']);
	});

	it('an empty queue yields no rows rather than throwing', () => {
		expect(annotatorMetrics([])).toEqual([]);
	});
});
