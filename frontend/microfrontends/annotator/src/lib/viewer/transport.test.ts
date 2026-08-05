/**
 * The temporal transport's arithmetic.
 *
 * Reported: "where is my the video and audio player and tools for annotating in video and audio?…
 * we need a good video annotation too aswell like cvat and label studio".
 *
 * Audio had exactly one control (play/pause). Video had play/pause, `mm:ss / mm:ss` and a seek
 * slider. These pin the rules the new controls run on — checkable without a decoded media file, a
 * GPU, or a running dev server, which is what the browser drive needs.
 */

import { describe, expect, it } from 'vitest';

import {
	RATES,
	clampTime,
	fmtTime,
	frameIndex,
	keyAction,
	nextRate,
	skip,
	stepFrame,
	transportShouldHandle,
} from './transport';

describe('clamping', () => {
	it('keeps a seek inside the media', () => {
		// `<video>.currentTime = -1` throws in some engines and silently clamps in others, and
		// wavesurfer's seekTo takes a 0..1 FRACTION — an out-of-range value scrolls the waveform
		// somewhere with no annotation on it.
		expect(clampTime(-5, 10)).toBe(0);
		expect(clampTime(99, 10)).toBe(10);
		expect(clampTime(4.2, 10)).toBe(4.2);
	});

	it('survives an UNKNOWN duration rather than clamping to zero', () => {
		// `duration` is NaN until metadata loads. Clamping to 0 there would yank the playhead to the
		// start on any key pressed during loading.
		expect(clampTime(3, Number.NaN)).toBe(3);
		expect(clampTime(-1, Number.NaN)).toBe(0);
	});

	it('treats a non-finite seek as the start', () => {
		expect(clampTime(Number.NaN, 10)).toBe(0);
	});
});

describe('skip', () => {
	it('moves forward and back, clamped at both ends', () => {
		expect(skip(10, 5, 60)).toBe(15);
		expect(skip(2, -5, 60)).toBe(0);
		expect(skip(58, 5, 60)).toBe(60);
	});
});

describe('frame stepping — the control CVAT is built around', () => {
	it('lands on a frame boundary, not on an offset from where you happened to be', () => {
		// The drift this prevents: stepping from 1.037s at 25fps without rounding first gives 1.077,
		// 1.117, … and never sits on a boundary. A box drawn there belongs to no frame.
		expect(stepFrame(1.037, 1, 25, 10)).toBeCloseTo(1.08, 5); // round(25.9)=26 -> 27/25
		expect(stepFrame(1.037, -1, 25, 10)).toBeCloseTo(1.0, 5); // 26-1=25 -> 25/25
	});

	it('steps by exactly one frame from a boundary', () => {
		expect(stepFrame(1.0, 1, 25, 10)).toBeCloseTo(1.04, 5);
		expect(stepFrame(1.0, -1, 25, 10)).toBeCloseTo(0.96, 5);
	});

	it('cannot step past either end', () => {
		expect(stepFrame(0, -1, 25, 10)).toBe(0);
		expect(stepFrame(10, 1, 25, 10)).toBe(10);
	});

	it('falls back to 25fps when the rate is unknown', () => {
		// HTML5 video exposes no frame rate. An unknown fps must still step by SOMETHING sensible
		// rather than dividing by zero and seeking to Infinity.
		expect(stepFrame(1, 1, 0, 10)).toBeCloseTo(1.04, 5);
		expect(stepFrame(1, 1, Number.NaN, 10)).toBeCloseTo(1.04, 5);
	});

	it('reports a frame INDEX a person can say out loud', () => {
		expect(frameIndex(0, 25)).toBe(0);
		expect(frameIndex(1, 25)).toBe(25);
		expect(frameIndex(16.48, 25)).toBe(412);
	});
});

describe('playback rate', () => {
	it('starts at 1 and cycles', () => {
		expect(RATES[0]).toBe(1);
		expect(nextRate(1)).toBe(1.5);
		expect(nextRate(2)).toBe(0.5);
	});

	it('wraps back to the start', () => {
		expect(nextRate(RATES[RATES.length - 1] as number)).toBe(1);
	});

	it('recovers from a rate that is not in the cycle', () => {
		// A media element can report 1.25 after an external change. `indexOf` gives -1, and -1+1=0,
		// so the cycle resumes at 1 rather than throwing or sticking.
		expect(nextRate(1.25)).toBe(1);
	});
});

describe('time display', () => {
	it('shows a TENTH, because segment boundaries are tenths apart', () => {
		// `mm:ss` renders two different boundaries as the same number, which is how an annotator
		// convinces themselves a segment is misplaced.
		expect(fmtTime(63.44)).toBe('1:03.4');
		expect(fmtTime(5)).toBe('0:05.0');
		expect(fmtTime(9.96)).toBe('0:10.0');
	});

	it('pads seconds under ten', () => {
		expect(fmtTime(61.2)).toBe('1:01.2');
	});

	it('ROLLS OVER when rounding crosses a minute', () => {
		// The sibling of the 9.96 bug: splitting before rounding renders 59.96 as "0:60.0", a time
		// that does not exist. One rounded total, then split, makes both impossible.
		expect(fmtTime(59.96)).toBe('1:00.0');
		expect(fmtTime(119.97)).toBe('2:00.0');
	});

	it('never renders a negative or NaN time', () => {
		expect(fmtTime(-1)).toBe('0:00.0');
		expect(fmtTime(Number.NaN)).toBe('0:00.0');
	});
});

describe('keyboard — ONE binding set for both surfaces', () => {
	it('space plays and pauses', () => {
		expect(keyAction(' ')).toEqual({ kind: 'playPause' });
	});

	it('bare arrows skip five seconds; SHIFT+arrow steps a frame', () => {
		// Matching CVAT: the bare arrow is the coarse move, the modifier is the finer one.
		expect(keyAction('ArrowRight')).toEqual({ kind: 'skip', delta: 5 });
		expect(keyAction('ArrowLeft')).toEqual({ kind: 'skip', delta: -5 });
		expect(keyAction('ArrowRight', { shift: true })).toEqual({ kind: 'frame', direction: 1 });
		expect(keyAction('ArrowLeft', { shift: true })).toEqual({ kind: 'frame', direction: -1 });
	});

	it('r cycles the rate', () => {
		expect(keyAction('r')).toEqual({ kind: 'rate' });
		expect(keyAction('R')).toEqual({ kind: 'rate' });
	});

	it('NEVER steals a Ctrl/Cmd/Alt combination', () => {
		// Those are save, undo and find. A transport that swallowed them would break the canvas it
		// sits under — `Ctrl+S` is the save this zone documents everywhere.
		expect(keyAction(' ', { ctrl: true })).toBeNull();
		expect(keyAction('ArrowRight', { meta: true })).toBeNull();
		expect(keyAction('r', { alt: true })).toBeNull();
	});

	it('ignores keys it does not own', () => {
		expect(keyAction('a')).toBeNull();
	});
});

describe('when the transport must stay OUT of the way', () => {
	const el = (tag: string, editable = false) => {
		const e = { tagName: tag, isContentEditable: editable } as unknown as EventTarget;
		return e;
	};

	it('does not fire while someone is typing', () => {
		// The sidebar is full of text inputs. A space that plays audio instead of typing a space is
		// how people stop using keyboard shortcuts entirely.
		expect(transportShouldHandle(el('INPUT'))).toBe(false);
		expect(transportShouldHandle(el('TEXTAREA'))).toBe(false);
		expect(transportShouldHandle(el('SELECT'))).toBe(false);
		expect(transportShouldHandle(el('DIV', true))).toBe(false);
	});

	it('fires everywhere else', () => {
		expect(transportShouldHandle(el('DIV'))).toBe(true);
		expect(transportShouldHandle(el('BUTTON'))).toBe(true);
		expect(transportShouldHandle(null)).toBe(true);
	});
});
