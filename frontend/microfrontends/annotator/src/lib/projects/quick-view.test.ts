/**
 * Reviewing a queue without leaving it.
 *
 * A pre-labelled queue is reviewed rather than drawn on, and doing that today means clicking through
 * to the canvas route and back for every item — losing the filter, the page and the selection each
 * time. Fifty-seven items, fifty-seven round trips through a page you did not want to leave.
 *
 * These pin the navigation, and the rule that matters is that it does NOT wrap: a review pass that
 * silently loops has no end, and no way to tell you that you have seen everything.
 */

import { describe, expect, it } from 'vitest';

import { afterVerdict, neighbours } from './quick-view';
import type { TaskDetail } from './types';

const t = (id: string) => ({ task_id: id }) as TaskDetail;
const page = [t('a'), t('b'), t('c')];

describe('where you are in the page', () => {
	it('reports a 1-based position and the total', () => {
		expect(neighbours(page, 'b')).toMatchObject({ position: 2, total: 3 });
	});

	it('gives both neighbours in the middle', () => {
		const n = neighbours(page, 'b');

		expect(n.prev?.task_id).toBe('a');
		expect(n.next?.task_id).toBe('c');
	});

	it('has no prev at the START and does not wrap to the end', () => {
		// Wrapping makes a review pass loop forever with nothing saying you have seen everything.
		const n = neighbours(page, 'a');

		expect(n.prev).toBeNull();
		expect(n.next?.task_id).toBe('b');
	});

	it('has no next at the END and does not wrap to the start', () => {
		const n = neighbours(page, 'c');

		expect(n.next).toBeNull();
		expect(n.prev?.task_id).toBe('b');
	});

	it('reports position 0 for a task that is not in view', () => {
		// It can happen for real: a filter change drops the open item out of the page. Position 0 is
		// what the caller reads as "close the drawer", rather than showing a stale item beside a
		// list that no longer contains it.
		expect(neighbours(page, 'zzz')).toMatchObject({ position: 0, prev: null, next: null });
	});

	it('handles nothing open', () => {
		expect(neighbours(page, null).position).toBe(0);
	});

	it('handles an empty page', () => {
		expect(neighbours([], 'a')).toEqual({ position: 0, total: 0, prev: null, next: null });
	});

	it('handles a single-item page — no prev, no next, but a real position', () => {
		expect(neighbours([t('only')], 'only')).toMatchObject({ position: 1, total: 1 });
		expect(neighbours([t('only')], 'only').next).toBeNull();
	});
});

describe('what to open after a verdict', () => {
	it('ADVANCES, because the point of a review pass is the next one', () => {
		expect(afterVerdict(page, 'a')).toBe('b');
	});

	it('returns null at the end, so the caller closes rather than looping', () => {
		// Pretending there is more to review would be a lie about the queue's state.
		expect(afterVerdict(page, 'c')).toBeNull();
	});

	it('returns null for an item no longer in view', () => {
		expect(afterVerdict(page, 'gone')).toBeNull();
	});
});
