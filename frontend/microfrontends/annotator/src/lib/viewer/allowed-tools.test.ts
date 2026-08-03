/**
 * The rail offers only what the task will accept.
 *
 * Enforcement was server-side only: all ten tools stayed available regardless of the task's
 * declared tools, so an annotator drew a polygon in a bbox-only task and learned at SUBMIT, after
 * the work — a 409 naming a rule the UI never showed. That is enforcement that punishes; declining
 * to offer the tool is enforcement that teaches, and the 409 stays as the backstop for any caller
 * that is not this canvas.
 *
 * `ProjectsLanding.svelte` asserted in a comment that "the canvas constrains its tools to it".
 * It did not. OPEN-WORK recorded the same thing as open. A comment cannot fail a build, which is
 * why this file exists.
 *
 * The two sides also speak different vocabularies — the task declares canonical shape types
 * (`bbox`, `polyline`) and the engine names tools after what they draw (`rect`, `line`) — so the
 * mapping is the load-bearing part, and it is one-to-many: `polygon` is reachable from the polygon,
 * magnetic, brush and pencil tools alike.
 */

import { describe, expect, it } from 'vitest';

import { COMMITS_SHAPE, engineToolsFor } from '@rask/labeling/shape-types';
import { TOOL_DEFS } from './tool-defs.js';

/** The rail's own predicate, mirrored: a drawing tool survives only if the task permits it. */
function railFor(allowed: string[]): string[] {
	const permitted = engineToolsFor(allowed);
	return TOOL_DEFS.filter(
		(t) => !COMMITS_SHAPE.has(t.tool) || permitted === null || permitted.has(t.tool),
	).map((t) => t.tool);
}

describe('a task restricts the rail', () => {
	it('a bbox-only task does NOT offer the polygon tool', () => {
		const rail = railFor(['bbox']);

		expect(rail, 'the polygon tool is offered by a task that refuses polygons').not.toContain(
			'polygon',
		);
		expect(rail, 'the rectangle tool is the one thing this task CAN draw').toContain('rect');
	});

	it('a bbox-only task hides every other drawing tool too', () => {
		const rail = railFor(['bbox']);
		for (const hidden of ['polygon', 'point', 'line', 'pencil', 'magnetic', 'brush']) {
			expect(rail, `${hidden} survives a bbox-only restriction`).not.toContain(hidden);
		}
	});

	it('navigation tools are never hidden by a restriction', () => {
		// select/pan/lasso do not COMMIT a shape, so no task rule should ever remove them — a rail
		// without select is unusable regardless of what the task permits.
		const rail = railFor(['bbox']);
		expect(rail).toContain('select');
		expect(rail).toContain('pan');
		expect(rail).toContain('lasso');
	});

	it('maps canonical shape types onto the ENGINE tool names', () => {
		// The seam that made the whole thing broken in the first place: the task says `polyline`,
		// the engine's tool is called `line`.
		expect(railFor(['polyline'])).toContain('line');
		expect(railFor(['keypoint'])).toContain('point');
		expect(railFor(['bbox'])).toContain('rect');
	});

	it('offers EVERY tool that can produce a permitted shape', () => {
		// One-to-many: hiding magnetic/brush/pencil from a polygon task would read as three broken
		// buttons rather than one honest constraint.
		const rail = railFor(['polygon']);
		for (const tool of ['polygon', 'magnetic', 'brush', 'pencil']) {
			expect(rail, `${tool} also commits a polygon and should be offered`).toContain(tool);
		}
	});

	it('an unconstrained task offers everything', () => {
		// The ad-hoc canvas (no task) must be unchanged by this feature.
		expect(engineToolsFor([])).toBeNull();
		expect(railFor([])).toEqual(TOOL_DEFS.map((t) => t.tool));
	});

	it('a task permitting only non-drawable types does not present an empty rail', () => {
		// A tag-only classification task has nothing to draw. Hiding every tool would read as a
		// broken canvas rather than a task that simply is not about drawing.
		expect(engineToolsFor(['tag'])).toBeNull();
	});
});
