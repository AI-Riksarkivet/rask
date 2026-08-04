/**
 * The geometry of a drawn relation.
 *
 * A relation is DIRECTED — which end is the key and which is the value is the one fact a KIE
 * reviewer is checking — so an arrowhead pointing the wrong way is a correctness bug, not a
 * cosmetic one. It should not take a WebGPU context to catch that, which is why the math lives
 * outside the Pixi calls.
 */

import { describe, expect, it } from 'vitest';

import { linkPath } from './ArrowDataPlugin.js';

/** The arrowhead's tip is at `b`; its base sits back along the line. This returns the base's centre,
 *  which is what tells us WHICH WAY the head points. */
function headCentre(p: NonNullable<ReturnType<typeof linkPath>>) {
	return { x: (p.leftX + p.rightX) / 2, y: (p.leftY + p.rightY) / 2 };
}

describe('linkPath', () => {
	it('the arrowhead points at the TARGET, not the source', () => {
		const p = linkPath(0, 0, 100, 0)!;
		const base = headCentre(p);

		// Pointing right: the base is BEHIND the tip, i.e. at a smaller x.
		expect(base.x).toBeLessThan(p.bx);
		expect(base.y).toBeCloseTo(0, 5);
	});

	it('reversing the endpoints reverses the head', () => {
		// The check that a bare line cannot make: draw key→value and value→key and they must differ.
		const forward = headCentre(linkPath(0, 0, 100, 0)!);
		const back = headCentre(linkPath(100, 0, 0, 0)!);

		expect(forward.x).toBeLessThan(100);
		expect(back.x).toBeGreaterThan(0);
		expect(forward.x).not.toBeCloseTo(back.x, 1);
	});

	it('the head sits ON the line, whatever the angle', () => {
		// A 45° edge: the base must stay on the segment, not drift off at an axis-aligned offset —
		// the failure you get from using dx/dy instead of the normalised vector.
		const p = linkPath(0, 0, 100, 100)!;
		const base = headCentre(p);

		expect(base.x).toBeCloseTo(base.y, 5);
		expect(base.x).toBeLessThan(100);
	});

	it('the head has WIDTH — the two base corners are apart', () => {
		const p = linkPath(0, 0, 100, 0)!;
		expect(Math.hypot(p.leftX - p.rightX, p.leftY - p.rightY)).toBeGreaterThan(1);
	});

	it('the head width is PERPENDICULAR to the line', () => {
		const p = linkPath(0, 0, 100, 0)!;
		// A horizontal edge spreads its head vertically.
		expect(p.leftX).toBeCloseTo(p.rightX, 5);
		expect(p.leftY).not.toBeCloseTo(p.rightY, 1);
	});

	it('two coincident centres draw NOTHING', () => {
		// Normalising a zero-length vector yields NaN, which Pixi renders as nothing while reporting
		// nothing — a silent non-draw is worse than an explicit one.
		expect(linkPath(50, 50, 50, 50)).toBeNull();
		expect(linkPath(50, 50, 50.2, 50.2)).toBeNull();
	});

	it('the line still spans the two centres it was given', () => {
		const p = linkPath(10, 20, 300, 400)!;
		expect([p.ax, p.ay, p.bx, p.by]).toEqual([10, 20, 300, 400]);
	});
});
