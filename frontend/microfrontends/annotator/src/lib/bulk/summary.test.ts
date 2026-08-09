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
			textId: 'd',
			predictionIds: ['d'],
			reviewer: '',
			reviewedAt: null,
		});
	});

	it('an empty table reads as empty, not as an error', () => {
		const t = tableFromArrays({ id: [] as string[] });
		expect(summarize(t)).toEqual({
			total: 0,
			byStatus: {},
			tags: [],
			text: '',
			textId: null,
			predictionIds: [],
			reviewer: '',
			reviewedAt: null,
		});
	});

	it('surfaces the actionable rows: every prediction id, and the excerpt row an edit targets', () => {
		const t = tableFromArrays({
			id: ['r0', 'r1', 'r2'],
			shape_type: ['bbox', 'bbox', 'bbox'],
			label: ['line', 'line', 'line'],
			status: ['prediction', 'accepted', 'prediction'],
			text: ['', 'therest aff', ''],
		});
		const s = summarize(t);
		expect(s.predictionIds).toEqual(['r0', 'r2']);
		expect(s.textId).toBe('r1');
		expect(s.text).toBe('therest aff');
	});

	it('attribution renders the NEWEST server stamp — reviewer of the max updated_at', () => {
		const t = tableFromArrays({
			id: ['a', 'b'],
			status: ['accepted', 'accepted'],
			reviewer: ['gina', 'omar'],
			updated_at: [1000, 2000],
		});
		const s = summarize(t);
		expect(s.reviewer).toBe('omar');
		expect(s.reviewedAt).toBe(2000);
	});

	it('a reviewer without a timestamp still beats nobody', () => {
		const t = tableFromArrays({
			id: ['a'],
			status: ['accepted'],
			reviewer: ['gina'],
		});
		const s = summarize(t);
		expect(s.reviewer).toBe('gina');
		expect(s.reviewedAt).toBeNull();
	});
});
