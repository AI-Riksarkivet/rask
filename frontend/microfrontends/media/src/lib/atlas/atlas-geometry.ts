/** 2-D point-in-polygon selection geometry in data space (lasso / box). Pure. */

export type Pt = { x: number; y: number };

/** Ray-cast even–odd test: is `(px, py)` inside `poly`? */
function pointInPolygon(px: number, py: number, poly: readonly Pt[]): boolean {
	let inside = false;
	for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
		const a = poly[i]!;
		const b = poly[j]!;
		if (a.y > py !== b.y > py && px < ((b.x - a.x) * (py - a.y)) / (b.y - a.y) + a.x) {
			inside = !inside;
		}
	}
	return inside;
}

/** Indices of all points (parallel `xs`/`ys`) that fall inside `poly`. */
export function indicesInPolygon(
	xs: Float32Array,
	ys: Float32Array,
	poly: readonly Pt[],
): number[] {
	const out: number[] = [];
	for (let i = 0; i < xs.length; i++) {
		if (pointInPolygon(xs[i]!, ys[i]!, poly)) out.push(i);
	}
	return out;
}
