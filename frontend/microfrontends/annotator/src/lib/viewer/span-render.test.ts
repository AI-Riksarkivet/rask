/**
 * The span surface's rendering model — segmentation and colour, checkable without a browser.
 */

import { describe, expect, it } from 'vitest';
import { type SpanMark, segmentText, spanColor } from './span-render';

const mark = (index: number, start: number, end: number, label = 'person'): SpanMark => ({
	index,
	id: `s${index}`,
	start,
	end,
	label,
});

const TEXT = 'Gustaf Eriksson Vasa reste till Dalarna';

describe('segmentText', () => {
	it('an unannotated text is ONE segment with no coverage', () => {
		const [seg, ...rest] = segmentText(TEXT, []);

		expect(rest).toHaveLength(0);
		expect(seg?.text).toBe(TEXT);
		expect(seg?.marks).toHaveLength(0);
	});

	it('a span cuts the text into before · inside · after', () => {
		const segs = segmentText(TEXT, [mark(1, 32, 39, 'place')]);

		expect(segs.map((s) => s.text)).toEqual(['Gustaf Eriksson Vasa reste till ', 'Dalarna']);
		expect(segs[0]?.marks).toHaveLength(0);
		expect(segs[1]?.marks.map((m) => m.label)).toEqual(['place']);
		// The label tag renders where the span ENDS.
		expect(segs[1]?.ending.map((m) => m.label)).toEqual(['place']);
	});

	it('OVERLAPPING spans come out as stacked coverage, never as broken ranges', () => {
		// person over "Gustaf Eriksson Vasa", dynasty over "Vasa" — coverage 1·1·2.
		const segs = segmentText(TEXT, [mark(1, 0, 20, 'person'), mark(2, 16, 20, 'dynasty')]);

		expect(segs.map((s) => s.marks.length)).toEqual([1, 2, 0]);
		expect(segs[1]?.text).toBe('Vasa');
		// Outermost first, so the surface nests deterministically.
		expect(segs[1]?.marks.map((m) => m.label)).toEqual(['person', 'dynasty']);
		// Both spans end at 20 — both tags render there, once each.
		expect(segs[1]?.ending.map((m) => m.label)).toEqual(['person', 'dynasty']);
	});

	it('a span outside the text is DROPPED, not clamped into meaning something else', () => {
		const segs = segmentText(TEXT, [mark(1, 30, 400), mark(2, 8, 3), mark(3, -2, 5)]);

		expect(segs).toHaveLength(1);
		expect(segs[0]?.marks).toHaveLength(0);
	});
});

describe('spanColor', () => {
	it('classes colour by DECLARED position, so a task keeps its colours stable', () => {
		const classes = ['person', 'place'];

		expect(spanColor('person', classes)).not.toBe(spanColor('place', classes));
		expect(spanColor('person', classes)).toBe(spanColor('person', classes));
	});

	it('a label outside the taxonomy still gets a stable colour (hash fallback)', () => {
		expect(spanColor('mystery', [])).toBe(spanColor('mystery', []));
	});
});
