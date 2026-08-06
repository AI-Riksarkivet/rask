/**
 * Reacting to an annotations change you did not make.
 *
 * #73: the Arrow table was read once, at mount. Two annotators on the same page never saw each
 * other's work; nor did one person with the item open in two tabs. The second save then went out
 * against a `base_version` from before the first — exactly the conflict OCC refuses — so their work
 * was rejected AFTER they did it.
 *
 * These pin the ONE rule that makes reacting safe: never reload over unsaved work.
 */

import { describe, expect, it } from 'vitest';

import { onRemoteChange, type CanvasState } from './remote-change';

const state = (over: Partial<CanvasState> = {}): CanvasState => ({
	dirty: false,
	saving: false,
	attached: true,
	...over,
});

describe('a clean canvas takes the newer truth', () => {
	it('reloads', () => {
		// Safe and invisible: nothing on screen is theirs yet, so replacing it loses nothing and
		// quietly fixes both the two-tab and the two-annotator case.
		expect(onRemoteChange(state())).toEqual({ action: 'reload' });
	});
});

describe('a DIRTY canvas is never reloaded — the whole ruling', () => {
	it('warns instead of discarding drawn shapes', () => {
		// The single worst outcome available to an annotation tool. Worse than the staleness it
		// fixes, because staleness is recoverable and lost work is not.
		const decision = onRemoteChange(state({ dirty: true }));

		expect(decision.action).toBe('warn');
	});

	it('says the SAVE WILL BE REFUSED, not merely that something changed', () => {
		// "Changed" alone leaves someone unable to decide. They need the consequence: their edits
		// survive on screen, and the save is going to be rejected until they reload.
		const decision = onRemoteChange(state({ dirty: true }));

		expect(decision.action === 'warn' && decision.reason).toMatch(/refused/i);
		expect(decision.action === 'warn' && decision.reason).toMatch(/unsaved/i);
	});
});

describe('cases where reacting at all would be wrong', () => {
	it('ignores a change while a save is IN FLIGHT', () => {
		// Our own write is about to move the version. Reloading mid-flight races it and can show the
		// pre-save state as though it were newer.
		expect(onRemoteChange(state({ saving: true }))).toEqual({ action: 'ignore' });
	});

	it('ignores a change when the controller is not ATTACHED', () => {
		// `ImageViewer` leaves it unattached whenever the annotations read failed (#72). Reloading
		// there would fight a load that is already broken.
		expect(onRemoteChange(state({ attached: false }))).toEqual({ action: 'ignore' });
	});

	it('prefers IGNORE over warn when unattached AND dirty', () => {
		// Unattached means nothing was ever bound, so `dirty` cannot describe real work. Warning
		// about edits that cannot exist would be noise on an already-broken canvas.
		expect(onRemoteChange(state({ attached: false, dirty: true }))).toEqual({ action: 'ignore' });
	});

	it('prefers IGNORE over warn while saving, even when dirty', () => {
		// Dirty-and-saving is the NORMAL state during a save. Warning there would fire on every
		// single save an annotator makes.
		expect(onRemoteChange(state({ saving: true, dirty: true }))).toEqual({ action: 'ignore' });
	});
});
