/**
 * Uniform spatial grid over the scatter's x/y extent for O(1)-ish hover picking.
 * Linear-scanning ~145k points on every pointermove is the dominant lag, so
 * point indices are bucketed into a GRID×GRID lattice once and a hover only
 * scans the target cell + widening neighbour rings. Pure (no Svelte/DOM).
 */

const GRID = 256;

export type SpatialGrid = {
	cells: number[][]; // row-major GRID*GRID buckets of point indices
	cols: number;
	rows: number;
	minX: number;
	minY: number;
	invW: number; // 1 / cellWidth
	invH: number; // 1 / cellHeight
};

/** Bucket every point (parallel `xs`/`ys`) into the lattice over its extent. */
export function buildGrid(xs: Float32Array, ys: Float32Array): SpatialGrid {
	let minX = Infinity;
	let minY = Infinity;
	let maxX = -Infinity;
	let maxY = -Infinity;
	for (let i = 0; i < xs.length; i++) {
		const xi = xs[i]!;
		const yi = ys[i]!;
		if (xi < minX) minX = xi;
		if (xi > maxX) maxX = xi;
		if (yi < minY) minY = yi;
		if (yi > maxY) maxY = yi;
	}
	if (!Number.isFinite(minX)) {
		minX = 0;
		minY = 0;
		maxX = 1;
		maxY = 1;
	}
	const spanX = maxX - minX || 1;
	const spanY = maxY - minY || 1;
	const invW = GRID / spanX;
	const invH = GRID / spanY;
	const cells: number[][] = Array.from({ length: GRID * GRID }, () => []);
	for (let i = 0; i < xs.length; i++) {
		const cx = Math.min(GRID - 1, Math.max(0, ((xs[i]! - minX) * invW) | 0));
		const cy = Math.min(GRID - 1, Math.max(0, ((ys[i]! - minY) * invH) | 0));
		cells[cy * GRID + cx]!.push(i);
	}
	return { cells, cols: GRID, rows: GRID, minX, minY, invW, invH };
}

/** Nearest point index to data `(qx, qy)`: scan the target cell + widening
 *  neighbour rings until a hit within the pick radius; `null` past it. */
export function nearestIndex(
	grid: SpatialGrid,
	xs: Float32Array,
	ys: Float32Array,
	qx: number,
	qy: number,
): number | null {
	const g = grid;
	const unit = 1 / Math.max(g.invW, g.invH); // ~one grid cell in data units
	const maxR = unit * 12;
	const cx = Math.min(g.cols - 1, Math.max(0, ((qx - g.minX) * g.invW) | 0));
	const cy = Math.min(g.rows - 1, Math.max(0, ((qy - g.minY) * g.invH) | 0));
	let best = -1;
	let bestD = Infinity;
	for (let r = 0; r < g.cols; r++) {
		const x0 = Math.max(0, cx - r);
		const x1 = Math.min(g.cols - 1, cx + r);
		const y0 = Math.max(0, cy - r);
		const y1 = Math.min(g.rows - 1, cy + r);
		for (let gy = y0; gy <= y1; gy++) {
			for (let gx = x0; gx <= x1; gx++) {
				if (r > 0 && gx > x0 && gx < x1 && gy > y0 && gy < y1) continue; // interior already scanned
				const bucket = g.cells[gy * g.cols + gx]!;
				for (const i of bucket) {
					const dx = xs[i]! - qx;
					const dy = ys[i]! - qy;
					const d = dx * dx + dy * dy;
					if (d < bestD) {
						bestD = d;
						best = i;
					}
				}
			}
		}
		const ringEdge = r / Math.max(g.invW, g.invH);
		if (best >= 0 && ringEdge * ringEdge > bestD) break;
		if (ringEdge > maxR) break;
	}
	if (best < 0 || Math.sqrt(bestD) > maxR) return null;
	return best;
}
