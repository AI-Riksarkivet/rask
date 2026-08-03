/**
 * The honest-lease contract (A2): a claimed task whose lease died is NOT shown as held.
 * These are the pure rules the queue chip renders from — the server stays the authority.
 */
import { describe, expect, it } from 'vitest';

import { formatLease, leaseView } from './lease.js';

const NOW = new Date('2026-07-31T12:00:00Z');

describe('leaseView', () => {
	it('a live lease is held, with seconds left and mine-ness', () => {
		const view = leaseView(
			{ state: 'claimed', assignee: 'gina', lease_expires_at: '2026-07-31T12:10:00Z' },
			'gina',
			NOW,
		);
		expect(view).toEqual({ kind: 'held', assignee: 'gina', secondsLeft: 600, mine: true });
	});

	it("someone else's live lease is held but not mine", () => {
		const view = leaseView(
			{ state: 'claimed', assignee: 'dave', lease_expires_at: '2026-07-31T12:10:00Z' },
			'gina',
			NOW,
		);
		expect(view.kind).toBe('held');
		expect(view.kind === 'held' && view.mine).toBe(false);
	});

	it('a lapsed lease is EXPIRED, never held — even before the server reminder fires', () => {
		const view = leaseView(
			{ state: 'claimed', assignee: 'gina', lease_expires_at: '2026-07-31T11:59:59Z' },
			'gina',
			NOW,
		);
		expect(view).toEqual({ kind: 'expired', assignee: 'gina' });
	});

	it('a manager-assigned task (null lease while claimed) is pinned, not expired', () => {
		const view = leaseView(
			{ state: 'claimed', assignee: 'dave', lease_expires_at: null },
			'gina',
			NOW,
		);
		expect(view).toEqual({ kind: 'pinned', assignee: 'dave' });
	});

	it('every non-claimed state is unheld regardless of stale fields', () => {
		for (const state of [
			'unassigned',
			'in_review',
			'changes_requested',
			'accepted',
			'skipped',
		] as const) {
			expect(
				leaseView(
					{ state, assignee: 'gina', lease_expires_at: '2026-07-31T12:10:00Z' },
					'gina',
					NOW,
				),
			).toEqual({
				kind: 'unheld',
			});
		}
	});
});

describe('formatLease', () => {
	it('renders mm:ss under an hour and h:mm:ss above', () => {
		expect(formatLease(90)).toBe('01:30');
		expect(formatLease(3661)).toBe('1:01:01');
		expect(formatLease(0)).toBe('00:00');
		expect(formatLease(-5)).toBe('00:00');
	});
});
