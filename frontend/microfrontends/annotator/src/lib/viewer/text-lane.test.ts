/**
 * The transcription lane reads the page the way a person does.
 */

import { describe, expect, it } from 'vitest';
import type { AnnoRow } from './annotation-rows';
import { textLaneLines } from './text-lane';

const row = (over: Partial<AnnoRow>): AnnoRow => ({
	index: 0,
	id: 'r',
	label: '',
	status: '',
	shape: 'bbox',
	group: '',
	text: '',
	source: '',
	confidence: null,
	uncertainty: null,
	tStart: null,
	tEnd: null,
	parentId: '',
	charStart: null,
	charEnd: null,
	y: null,
	metadata: '{}',
	order: null,
	...over,
});

describe('textLaneLines', () => {
	it('lines order TOP-TO-BOTTOM by page position, not by table (write) order', () => {
		const lines = textLaneLines([
			row({ index: 0, id: 'bottom', text: 'tredje raden', y: 300 }),
			row({ index: 1, id: 'top', text: 'första raden', y: 40 }),
			row({ index: 2, id: 'middle', text: 'andra raden', y: 160 }),
		]);

		expect(lines.map((l) => l.id)).toEqual(['top', 'middle', 'bottom']);
	});

	it('a SPAN is a range into another row’s text — never a line of its own', () => {
		const lines = textLaneLines([
			row({ index: 0, id: 'line', text: 'Gustaf Eriksson Vasa', y: 40 }),
			row({ index: 1, id: 'span', text: '', parentId: 'line', charStart: 0, charEnd: 6 }),
		]);

		expect(lines.map((l) => l.id)).toEqual(['line']);
	});

	it('rows without text are not lines; rows without a position keep table order, last', () => {
		const lines = textLaneLines([
			row({ index: 0, id: 'shape-only', y: 10 }),
			row({ index: 1, id: 'floating-b', text: 'b' }),
			row({ index: 2, id: 'positioned', text: 'a', y: 50 }),
			row({ index: 3, id: 'floating-c', text: 'c' }),
		]);

		expect(lines.map((l) => l.id)).toEqual(['positioned', 'floating-b', 'floating-c']);
	});
});
