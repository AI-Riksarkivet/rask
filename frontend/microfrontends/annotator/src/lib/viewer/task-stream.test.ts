/**
 * The label stream.
 *
 * Reported: "in anno i can't even naviagte between the claimed labling items???"
 *
 * The canvas could page WITHIN an item (`PageNav` over `keys`) and could not reach the next item you
 * had claimed. These pin the rules that make "next" always a legal move: whose items are in the
 * stream, that the queue's order wins, and that the ends are ends.
 */

import { describe, expect, it } from 'vitest';

import {
	claimedStream,
	neighbour,
	streamIndex,
	streamPosition,
	taskCanvasHref,
	type StreamTask,
} from './task-stream';

const task = (id: string, extra: Partial<StreamTask> = {}): StreamTask => ({
	task_id: id,
	state: 'claimed',
	assignee: 'me',
	source: { keys: [`vol/${id}/1`], where: 'transcripts_v2' },
	...extra,
});

const QUEUE: StreamTask[] = [
	task('a'),
	task('b', { assignee: 'someone-else' }),
	task('c'),
	task('d', { state: 'unassigned', assignee: null }),
	task('e'),
];

describe('whose items are in the stream', () => {
	it('walks only what YOU claimed', () => {
		// Not the whole queue. A stream over every task steps you into an item another annotator holds
		// a lease on: the canvas opens, you draw, and the save is refused server-side. Claimed-by-me is
		// the set where "next" is always a legal move.
		expect(claimedStream(QUEUE, 'me').map((t) => t.task_id)).toEqual(['a', 'c', 'e']);
	});

	it('excludes a task claimed by SOMEONE ELSE', () => {
		expect(claimedStream(QUEUE, 'me').map((t) => t.task_id)).not.toContain('b');
	});

	it('excludes an UNASSIGNED task even though nobody holds it', () => {
		// Unclaimed is not yours. Opening it on the canvas would annotate an item you never claimed,
		// which is the race the claim exists to prevent.
		expect(claimedStream(QUEUE, 'me').map((t) => t.task_id)).not.toContain('d');
	});

	it('is EMPTY when auth is ON and the identity is unresolved', () => {
		// The dangerous default. Other principals exist, so guessing hands you someone else's item;
		// an empty stream degrades to the old no-cross-item-nav behaviour, which is honest.
		expect(claimedStream(QUEUE, null)).toEqual([]);
	});

	it('walks EVERY claimed item when auth is OFF — one anonymous principal', () => {
		// The bug the first cut shipped. Gating purely on `me` made the stream permanently empty on an
		// ungoverned stack, because `capi/v1/me` 404s when auth is disabled — the feature would have
		// been invisible on exactly the stack it was demonstrated on, indistinguishable from never
		// having been built. "No identity" is two conditions and only one is dangerous.
		expect(claimedStream(QUEUE, null, false).map((t) => t.task_id)).toEqual(['a', 'b', 'c', 'e']);
	});

	it('still excludes UNASSIGNED items with auth off — claimed is claimed', () => {
		// Auth-off widens WHOSE items count, never WHICH states do. An unclaimed task is not work in
		// progress for anyone.
		expect(claimedStream(QUEUE, null, false).map((t) => t.task_id)).not.toContain('d');
	});

	it('prefers the real identity over the anonymous fallback when both are present', () => {
		// authEnabled=true + a resolved subject is the governed path and must not be widened.
		expect(claimedStream(QUEUE, 'me', true).map((t) => t.task_id)).toEqual(['a', 'c', 'e']);
	});
});

describe('the order is the QUEUE’s', () => {
	it('filters without sorting', () => {
		// The queue is already sorted — by the column you picked, with the filters you set. Re-sorting
		// here would make "next" on the canvas and "next row" in the table different items, with no
		// way to see which order won.
		const reversed = [task('z'), task('y'), task('x')];

		expect(claimedStream(reversed, 'me').map((t) => t.task_id)).toEqual(['z', 'y', 'x']);
	});
});

describe('moving through the stream', () => {
	const stream = claimedStream(QUEUE, 'me'); // a, c, e

	it('steps forward and back over the FILTERED set, skipping what was excluded', () => {
		// The real assertion: `b` sits between `a` and `c` in the raw queue and must not be reachable.
		expect(neighbour(stream, 'a', 1)?.task_id).toBe('c');
		expect(neighbour(stream, 'c', -1)?.task_id).toBe('a');
	});

	it('does NOT wrap at either end', () => {
		// Wrapping hides the end of the queue: press next, land where you started, and you cannot tell
		// "you have been round" from "nothing moved".
		expect(neighbour(stream, 'e', 1)).toBeNull();
		expect(neighbour(stream, 'a', -1)).toBeNull();
	});

	it('gives a task OUTSIDE the stream no neighbours at all', () => {
		// The canvas can be opened on an item you do not hold — from the corpus browser, or after a
		// lease lapsed. Teleporting to someone's first claimed task is the worst possible answer to
		// pressing an arrow.
		expect(streamIndex(stream, 'b')).toBe(-1);
		expect(neighbour(stream, 'b', 1)).toBeNull();
		expect(neighbour(stream, 'b', -1)).toBeNull();
	});

	it('gives no neighbours when no task is open', () => {
		expect(neighbour(stream, null, 1)).toBeNull();
	});
});

describe('the URL a stream hop navigates to', () => {
	it('carries `project` beside `task`, so the EXIT still reaches the queue', () => {
		// The bug fixed one commit ago, and the one this feature could reintroduce: without `project`
		// the exit falls back to the corpus browser, so hopping twice would lose your queue again.
		const href = taskCanvasHref(task('c'), 'bind86', '/annotator');

		expect(href).toContain('task=c');
		expect(href).toContain('project=bind86');
	});

	it('carries the task’s OWN dataset and keys', () => {
		// Items in one project can come from different corpora, so the hop cannot inherit the URL it
		// came from — it must be rebuilt from the destination task.
		const href = taskCanvasHref(
			{ task_id: 'q', state: 'claimed', assignee: 'me', source: { keys: ['k/1/1', 'k/1/2'], where: 'other_corpus' } },
			'p1',
		);

		expect(href).toContain('dataset=other_corpus');
		expect(href).toContain(`keys=${encodeURIComponent('k/1/1,k/1/2')}`);
	});

	it('omits the dataset when the task declares none, rather than sending an empty one', () => {
		const href = taskCanvasHref(
			{ task_id: 'q', state: 'claimed', assignee: 'me', source: { keys: ['k'], where: null } },
			'p1',
		);

		expect(href).not.toContain('dataset=');
	});

	it('percent-encodes ids that need it', () => {
		const href = taskCanvasHref(task('a b'), 'proj/1');

		expect(href).toContain('task=a%20b');
		expect(href).toContain('project=proj%2F1');
	});
});

describe('what the canvas renders', () => {
	const stream = claimedStream(QUEUE, 'me'); // a, c, e

	it('reports a 1-based position over the stream, not the raw queue', () => {
		// `c` is index 2 of 5 in the queue and 2 of 3 in the stream. Showing "3 / 5" would count items
		// the arrows cannot reach.
		const at = streamPosition(stream, 'c', 'bind86');

		expect(at.position).toBe(2);
		expect(at.total).toBe(3);
	});

	it('is INACTIVE when the canvas was not opened from a queue', () => {
		// No project → the corpus browser opened this canvas. Rendering "0 / 0" there advertises a
		// feature that does not apply.
		expect(streamPosition(stream, 'c', null).active).toBe(false);
	});

	it('is INACTIVE for an item outside the stream, even inside a project', () => {
		expect(streamPosition(stream, 'b', 'bind86').active).toBe(false);
	});

	it('offers no prev at the FIRST item and no next at the LAST', () => {
		const first = streamPosition(stream, 'a', 'bind86');
		const last = streamPosition(stream, 'e', 'bind86');

		expect(first.prevHref).toBeNull();
		expect(first.nextHref).not.toBeNull();
		expect(last.nextHref).toBeNull();
		expect(last.prevHref).not.toBeNull();
	});

	it('respects the zone base in both directions', () => {
		const at = streamPosition(stream, 'c', 'bind86', '/annotator');

		expect(at.prevHref?.startsWith('/annotator/?')).toBe(true);
		expect(at.nextHref?.startsWith('/annotator/?')).toBe(true);
	});
});
