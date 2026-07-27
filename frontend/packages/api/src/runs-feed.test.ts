import { describe, expect, it } from 'vitest';
import { NOTICE_WINDOW, orderForNotice } from './runs-feed.js';

/**
 * The ordering rule, pinned where it is APPLIED.
 *
 * This exists because the same rule was once written in the component instead, and the component never
 * sees the runs it would have reordered: the list is cut to `NOTICE_WINDOW` on the server. Live, 891 runs
 * with the first FAIL at position 445 — so a recency-only trim handed the bell twenty successes and no
 * amount of sorting downstream could have surfaced a failure. Sorting after slicing is decoration.
 */
const run = (id: string, state: string, updated: string) => ({
	run_id: id,
	state,
	updated_at: updated,
});

describe('orderForNotice', () => {
	it('lifts a failure above newer completions, however far down it started', () => {
		const runs = [
			...Array.from({ length: 30 }, (_, i) =>
				run(`ok-${i}`, 'COMPLETE', `2026-07-26T12:${String(i).padStart(2, '0')}:00Z`),
			),
			run('bad', 'FAIL', '2026-07-20T09:00:00Z'),
		];

		const notice = orderForNotice(runs, 5);

		expect(notice[0]?.run_id).toBe('bad');
		expect(notice).toHaveLength(5);
	});

	it('treats ABORT as a failure too — a run killed mid-flight is not a success', () => {
		const notice = orderForNotice(
			[
				run('done', 'COMPLETE', '2026-07-26T12:00:00Z'),
				run('killed', 'ABORT', '2026-07-01T00:00:00Z'),
			],
			2,
		);
		expect(notice.map((r) => r.run_id)).toEqual(['killed', 'done']);
	});

	it('orders the rest newest-first', () => {
		const notice = orderForNotice(
			[
				run('old', 'COMPLETE', '2026-07-20T00:00:00Z'),
				run('new', 'COMPLETE', '2026-07-26T00:00:00Z'),
				run('mid', 'RUNNING', '2026-07-24T00:00:00Z'),
			],
			3,
		);
		expect(notice.map((r) => r.run_id)).toEqual(['new', 'mid', 'old']);
	});

	it('is stable on missing timestamps and states rather than throwing', () => {
		const notice = orderForNotice(
			[{ run_id: 'a' }, { state: null, updated_at: null, run_id: 'b' }],
			2,
		);
		expect(notice).toHaveLength(2);
	});

	it('defaults to the notice window, not the whole board', () => {
		const runs = Array.from({ length: 100 }, (_, i) =>
			run(`r-${i}`, 'COMPLETE', `2026-07-26T00:00:${i}Z`),
		);
		expect(orderForNotice(runs)).toHaveLength(NOTICE_WINDOW);
	});
});
