/**
 * The canvas-row → task-draft-shape mapping (S10 first half). The two schemas are aligned
 * column-for-column on purpose; this pins the copy so a rename on either side is a named
 * failure here, not shapes silently missing from a publish.
 */
import { tableFromArrays } from 'apache-arrow';
import { describe, expect, it } from 'vitest';

import { rowsToShapes } from './draft-shapes.js';

describe('rowsToShapes', () => {
	it('maps the annotation columns onto draft shapes', () => {
		const table = tableFromArrays({
			id: ['a1', 'a2'],
			shape_type: ['bbox', 'polygon'],
			x: Float32Array.from([10, 0]),
			y: Float32Array.from([12, 0]),
			width: Float32Array.from([80, 0]),
			height: Float32Array.from([40, 0]),
			label: ['portrait', 'stamp'],
			difficult: [false, true],
		});

		const shapes = rowsToShapes(table);

		expect(shapes).toHaveLength(2);
		expect(shapes[0]).toMatchObject({
			shape_id: 'a1',
			shape_type: 'bbox',
			x: 10,
			y: 12,
			width: 80,
			height: 40,
			label: 'portrait',
			difficult: false,
		});
		expect(shapes[1]).toMatchObject({ shape_id: 'a2', shape_type: 'polygon', difficult: true });
	});

	it('skips rows without a shape_type rather than fabricating geometry', () => {
		const table = tableFromArrays({
			id: ['t1'],
			shape_type: [''],
			label: ['tag-only'],
		});
		expect(rowsToShapes(table)).toHaveLength(0);
	});
});
