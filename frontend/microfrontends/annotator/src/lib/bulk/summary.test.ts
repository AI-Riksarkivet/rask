/** The grid cell's projection of an item's annotations — pinned pure, like the preview model. */

import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it } from 'vitest';
import { summarize } from './summary.js';

describe('summarize', () => {
	it('counts by status, collects item tags, and excerpts the first transcription', () => {
		const t = tableFromArrays({
			id: ['a', 'b', 'c', 'd'],
			shape_type: ['bbox', 'tag', 'tag', 'bbox'],
			label: ['paragraph', 'damaged', 'faded-ink', 'stamp'],
			status: ['accepted', 'accepted', '', 'prediction'],
			text: ['', '', '', 'Anno 1632'],
		});
		expect(summarize(t)).toEqual({
			total: 4,
			byStatus: { accepted: 2, unlabelled: 1, prediction: 1 },
			tags: ['damaged', 'faded-ink'],
			text: 'Anno 1632',
		});
	});

	it('an empty table reads as empty, not as an error', () => {
		const t = tableFromArrays({ id: [] as string[] });
		expect(summarize(t)).toEqual({ total: 0, byStatus: {}, tags: [], text: '' });
	});
});
