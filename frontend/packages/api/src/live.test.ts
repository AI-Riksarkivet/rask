/**
 * The liveness reader's scheduling rules.
 *
 * These rules existed in three hand-copied files and were verified NOWHERE directly. The only test
 * that touched them was `lakehouse/e2e/lineage/live-refresh.spec.ts`, which counts HTTP reads in a
 * browser — a real test, and a slow, distant one: rule three was learned from an e2e failure in one
 * zone, on one plane, and the two other copies of the rule were never exercised at all. One of them
 * had already lost the comment explaining it.
 *
 * The decisions are pure, so they are pinned here, next to the code, where a zone adopting
 * `@rask/api/live` gets them verified rather than inherited.
 */

import { describe, expect, it } from 'vitest';

import { cursorMoved, shouldRead } from './live.svelte';

describe('the first read is unconditional', () => {
	it('reads before any cursor has connected', () => {
		// A surface must render immediately rather than waiting for a stream. A surface whose cursor
		// NEVER connects still shows its data and its own honest offline state — which is the whole
		// reason this is not gated on `connected`.
		expect(shouldRead({ started: false, keyChanged: false, cursorMoved: false })).toBe(true);
	});

	it('does not read again once started and nothing has changed', () => {
		// The property that makes this not-a-timer: an idle surface issues exactly one request and
		// then stays quiet.
		expect(shouldRead({ started: true, keyChanged: false, cursorMoved: false })).toBe(false);
	});
});

describe('a navigation re-reads at once', () => {
	it('reads when the key changed, without waiting for the estate to move', () => {
		// `key` is a route param or a selection. Waiting for a cursor advance here would leave a
		// freshly-opened page showing the previous page's data until something unrelated happened.
		expect(shouldRead({ started: true, keyChanged: true, cursorMoved: false })).toBe(true);
	});
});

describe('a cursor advance re-reads', () => {
	it('reads when the cursor moved', () => {
		expect(shouldRead({ started: true, keyChanged: false, cursorMoved: true })).toBe(true);
	});
});

describe('the cursor ARRIVING is not a change — the rule an e2e read-count caught', () => {
	it('treats undefined → first value as NOT a move', () => {
		// The defect: `.current` is undefined until the stream delivers its first value. Counting
		// `undefined → 137` as an advance makes EVERY page load read twice — once eagerly, once when
		// the stream lands — for no new information. `live-refresh.spec.ts` expected 1 read and got 2.
		expect(cursorMoved(undefined, 137)).toBe(false);
	});

	it('treats a later advance as a move', () => {
		expect(cursorMoved(137, 138)).toBe(true);
	});

	it('treats the SAME value arriving again as no move', () => {
		// A reconnect can redeliver the current value. Re-reading on that is the same wasted round
		// trip, arriving by a different door.
		expect(cursorMoved(137, 137)).toBe(false);
	});

	it('treats a cursor going BACKWARDS as a move', () => {
		// `!==`, not `>`. A cursor that decreases means the stream is talking about a different
		// history — a restarted plane, or a different upstream. Ignoring it because it is not an
		// increase would leave the surface pinned to data that no longer exists.
		expect(cursorMoved(138, 137)).toBe(true);
	});

	it('treats a cursor DISAPPEARING as a move', () => {
		// Established, then gone: the stream dropped. That is a state change worth re-reading on,
		// and it is distinguishable from never-having-arrived precisely because `previous` is set.
		expect(cursorMoved(137, undefined)).toBe(true);
	});

	it('is not fooled by zero', () => {
		// The falsy-vs-undefined trap. Cursor 0 is a real cursor; a truthiness check here would make
		// the very first sequence of a fresh plane invisible.
		expect(cursorMoved(undefined, 0)).toBe(false);
		expect(cursorMoved(0, 1)).toBe(true);
	});
});
