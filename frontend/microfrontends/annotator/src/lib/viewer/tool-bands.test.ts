/**
 * The rail reads in BANDS: navigate · draw · assist.
 *
 * Ten buttons in one undifferentiated column asked the annotator to remember which of them commits a
 * shape and which only moves the view. The complaint was never the rail's POSITION — a 44px left
 * strip is the right answer and always was — it was that its contents had no order and no grouping.
 *
 * The band a tool belongs to is a property of the tool, so the order is DERIVED. That matters
 * because `drawing` was doing double duty: `lasso` carries `drawing: true` (it is an edit-mode
 * affordance) while committing no shape at all — it selects. Grouping by what a tool is FOR is the
 * distinction a rail needs, and no amount of reordering a flat list expresses it.
 */

import { describe, expect, it } from 'vitest';

import { COMMITS_SHAPE } from '@rask/labeling/shape-types';
import { TOOL_DEFS, TOOL_GROUPS, TOOL_KEYS, bandsOf as bands } from './tool-defs.js';

describe('the tool rail reads in bands', () => {
	it('every tool declares a band', () => {
		// A tool with no band would silently vanish from a rail that renders bands.
		for (const t of TOOL_DEFS) {
			expect(TOOL_GROUPS, `${t.tool} has no band`).toContain(t.group);
		}
	});

	it('the registry is ORDERED by band — the rail never re-sorts it', () => {
		// The order is data, not markup. If the registry drifts out of band order, a rail that trusts
		// it would interleave, and a rail that sorts it would be a second source of truth.
		const seen = TOOL_DEFS.map((t) => TOOL_GROUPS.indexOf(t.group));
		expect(seen, 'TOOL_DEFS is not in band order').toEqual([...seen].sort((a, b) => a - b));
	});

	it('LASSO is navigate, not draw — it commits no shape', () => {
		// The exact confusion the flat list hid: `drawing: true` yet absent from COMMITS_SHAPE.
		const lasso = TOOL_DEFS.find((t) => t.tool === 'lasso')!;
		expect(lasso.drawing, 'lasso is still an edit-mode affordance').toBe(true);
		expect(COMMITS_SHAPE.has('lasso'), 'lasso commits a shape?').toBe(false);
		expect(lasso.group).toBe('navigate');
	});

	it('NAVIGATE commits nothing; draw and assist both commit', () => {
		// The precise rule, and my first version of this test got it wrong: `magnetic` commits a
		// polygon AND is machine-assisted, so "only draw commits" is false. The band says HOW you
		// draw — by hand, or with the corner pipeline helping — not WHETHER you draw. The invariant
		// that actually holds is about navigate, and it still catches a mis-banded tool.
		for (const t of TOOL_DEFS) {
			const commits = COMMITS_SHAPE.has(t.tool);
			if (t.group === 'navigate') {
				expect(commits, `${t.tool} is in navigate but commits a shape`).toBe(false);
			} else {
				expect(commits, `${t.tool} is in ${t.group} but commits nothing`).toBe(true);
			}
		}
	});

	it('ASSIST is exactly the machine-assisted tools', () => {
		// One tool today (corner-snap). Pinned so a second CV tool lands in the right band, and so a
		// plain tool cannot drift into assist and imply a pipeline it does not use.
		for (const t of TOOL_DEFS) {
			expect(Boolean(t.cv), `${t.tool} cv/band mismatch`).toBe(t.group === 'assist');
		}
	});

	it('hotkeys read top-to-bottom, so the rail is not a lookup puzzle', () => {
		// The half-fix this avoids: regrouping without renumbering leaves 1,2,7 | 3,4,5,6,8,B | 9.
		const digits = TOOL_DEFS.map((t) => t.key).filter((k) => /^\d$/.test(k));
		expect(digits).toEqual([...digits].sort());
	});

	it('every hotkey is unique — a duplicate silently loses a tool', () => {
		// TOOL_KEYS is an object keyed by hotkey, so a collision drops one WITHOUT any error.
		expect(Object.keys(TOOL_KEYS)).toHaveLength(TOOL_DEFS.length);
	});

	it('an EMPTY band is dropped, separator and all', () => {
		// The load-bearing case. A bbox-only task hides every assist tool, and a separator rendered
		// for an absent band is a hairline with nothing on either side of it.
		const bboxOnly = TOOL_DEFS.filter((t) => t.group !== 'assist' && t.tool !== 'polygon');
		const result = bands(bboxOnly);

		expect(result.map((b) => b.group)).toEqual(['navigate', 'draw']);
		expect(result.every((b) => b.tools.length > 0)).toBe(true);
	});

	it('a rail with ONE surviving band renders no separator at all', () => {
		// Separators go BETWEEN bands. One band means zero separators, not one leading hairline.
		const navOnly = TOOL_DEFS.filter((t) => t.group === 'navigate');
		expect(bands(navOnly)).toHaveLength(1);
	});

	it('bands come out in reading order regardless of filter order', () => {
		const shuffled = [...TOOL_DEFS].reverse();
		expect(bands(shuffled).map((b) => b.group)).toEqual(['navigate', 'draw', 'assist']);
	});
});
