/**
 * Shi–Tomasi corner detection over a still image — the whole pipeline behind the magnetic
 * (corner-snap) tool, in plain TypeScript over typed arrays.
 *
 * This replaces `@techstark/opencv-js`. That dependency cost the annotator 3.9 MB gzipped
 * (14.8 MB decoded) in one lazily-imported chunk, plus two more copies of the same 16 MB
 * module in the container image, and 1.6 s of parse/compile on loopback (2.6 s at 20 Mbit/s,
 * 4.3 s on 4G) the first time a user reached for the tool. What it bought was exactly two
 * calls — `cvtColor(src, dst, COLOR_RGBA2GRAY, 0)` and
 * `cornerMinEigenVal(gray, dst, 5, 3, BORDER_DEFAULT)`. Everything downstream (the quality
 * threshold, the 3×3 local-maximum test, the min-distance suppression) was already hand
 * written here, because `goodFeaturesToTrack` was never compiled into that build.
 *
 * So the two ops are implemented directly. Both follow OpenCV's algorithm exactly, and
 * `corners.test.ts` pins them to OpenCV's own recorded output (`corners.golden.json`):
 * the response map agrees to float32 rounding (2.1e-7 relative) and — the part that is
 * actually user-visible — `pickCorners` returns the identical corner set.
 *
 * Cost of the swap: the response map is computed in JS rather than wasm SIMD, so the
 * one-time-per-image detection goes from 52 ms to 217 ms at 1080p. That runs once, behind
 * the toolbar's existing loading state, against 1.6–4.3 s of module download-and-compile
 * it removes.
 */

/** Border extrapolation index for `BORDER_REFLECT_101` (OpenCV's `BORDER_DEFAULT`):
 *  `gfedcb|abcdefgh|gfedcba` — reflection that does NOT repeat the edge sample. */
function reflect101(i: number, n: number): number {
	if (n === 1) return 0;
	let j = i;
	while (j < 0 || j >= n) {
		if (j < 0) j = -j;
		if (j >= n) j = 2 * (n - 1) - j;
	}
	return j;
}

/**
 * RGBA → 8-bit grayscale, matching `cv.cvtColor(src, dst, cv.COLOR_RGBA2GRAY, 0)`.
 *
 * OpenCV 5 uses the ITU-R BT.601 luma weights and rounds to nearest; verified against its
 * output over 20 000 random pixels (2 disagreed by one level, both at an exact .5
 * boundary) and over the committed fixture (16 of 76 800 pixels, none by more than one
 * level — which changed no detected corner at all).
 */
export function rgbaToGray(rgba: Uint8Array | Uint8ClampedArray, pixels: number): Uint8Array {
	const gray = new Uint8Array(pixels);
	for (let i = 0; i < pixels; i++) {
		const p = i * 4;
		gray[i] = Math.round(rgba[p] * 0.299 + rgba[p + 1] * 0.587 + rgba[p + 2] * 0.114);
	}
	return gray;
}

/**
 * The Shi–Tomasi corner response map, matching
 * `cv.cornerMinEigenVal(gray, dst, blockSize, ksize, cv.BORDER_DEFAULT)`.
 *
 * Per OpenCV's `cornerEigenValsVecs`: scaled Sobel derivatives, the structure tensor
 * `(Ix², IxIy, Iy²)` summed (NOT averaged) over a `blockSize × blockSize` window, then the
 * smaller eigenvalue of that 2×2 matrix — `(a + c) − √((a − c)² + b²)` with `a`/`c` halved.
 *
 * The Sobel scale factor `1 / (2^(ksize−1) · blockSize · 255)` is OpenCV's, and reproducing
 * it is what makes the recorded fixture comparable. It is irrelevant to the corners
 * themselves: `pickCorners` thresholds on a fraction of the map's own maximum and tests for
 * local maxima, both invariant under a positive scale.
 */
export function cornerMinEigenVal(
	gray: Uint8Array,
	width: number,
	height: number,
	blockSize = 5,
	ksize = 3,
): Float32Array {
	const scale = 1 / ((1 << (ksize - 1)) * blockSize * 255);
	// Sobel 3×3: d/dx kernel [-1 0 1; -2 0 2; -1 0 1], d/dy its transpose. The weight of
	// tap (i, j) is `i · (2 − |j|)` for x and `j · (2 − |i|)` for y — the separable
	// [1 2 1] smoothing across the differencing axis, written inline.
	const dx = new Float32Array(width * height);
	const dy = new Float32Array(width * height);
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			let gx = 0;
			let gy = 0;
			for (let j = -1; j <= 1; j++) {
				const row = reflect101(y + j, height) * width;
				for (let i = -1; i <= 1; i++) {
					const v = gray[row + reflect101(x + i, width)];
					gx += v * (i * (2 - Math.abs(j)));
					gy += v * (j * (2 - Math.abs(i)));
				}
			}
			dx[y * width + x] = gx * scale;
			dy[y * width + x] = gy * scale;
		}
	}

	const radius = blockSize >> 1;
	const response = new Float32Array(width * height);
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			let sxx = 0;
			let sxy = 0;
			let syy = 0;
			for (let j = -radius; j <= radius; j++) {
				const row = reflect101(y + j, height) * width;
				for (let i = -radius; i <= radius; i++) {
					const k = row + reflect101(x + i, width);
					const a = dx[k];
					const b = dy[k];
					sxx += a * a;
					sxy += a * b;
					syy += b * b;
				}
			}
			const a = sxx * 0.5;
			const b = sxy;
			const c = syy * 0.5;
			response[y * width + x] = a + c - Math.sqrt((a - c) * (a - c) + b * b);
		}
	}
	return response;
}

/**
 * Shi–Tomasi corner picking over a min-eigenvalue response map: keep local maxima
 * above `quality × max(response)`, suppressing neighbors within `minDist` via a
 * coarse occupancy grid. This is `goodFeaturesToTrack`'s selection rule, which was
 * never compiled into the opencv-js build even when that build was present. Border
 * rows/cols are excluded from candidacy (the 3×3 max test needs a full neighborhood) —
 * same practical effect as goodFeaturesToTrack's aperture border; snapping falls to the
 * nearest interior corner.
 */
export function pickCorners(
	response: Float32Array,
	cols: number,
	rows: number,
	quality: number,
	minDist: number,
): [number, number][] {
	let max = 0;
	for (let i = 0; i < response.length; i++) if (response[i] > max) max = response[i];
	const threshold = max * quality;
	if (threshold <= 0) return [];

	// Candidates = 3×3 local maxima above the quality threshold, strongest first.
	const candidates: [number, number, number][] = []; // [response, x, y]
	for (let y = 1; y < rows - 1; y++) {
		for (let x = 1; x < cols - 1; x++) {
			const v = response[y * cols + x];
			if (v < threshold) continue;
			let isMax = true;
			for (let dy = -1; dy <= 1 && isMax; dy++) {
				for (let dx = -1; dx <= 1; dx++) {
					if ((dx || dy) && response[(y + dy) * cols + (x + dx)] > v) {
						isMax = false;
						break;
					}
				}
			}
			if (isMax) candidates.push([v, x, y]);
		}
	}
	candidates.sort((a, b) => b[0] - a[0]);

	// Greedy min-distance suppression on a cell grid (cell = minDist ⇒ only the
	// 3×3 neighborhood needs checking).
	const cell = Math.max(1, minDist);
	const gw = Math.ceil(cols / cell);
	const occupied = new Map<number, [number, number][]>();
	const out: [number, number][] = [];
	const minDistSq = minDist * minDist;
	for (const [, x, y] of candidates) {
		const cx = Math.floor(x / cell);
		const cy = Math.floor(y / cell);
		let ok = true;
		for (let ny = cy - 1; ny <= cy + 1 && ok; ny++) {
			for (let nx = cx - 1; nx <= cx + 1; nx++) {
				for (const [px, py] of occupied.get(ny * gw + nx) ?? []) {
					const ddx = px - x;
					const ddy = py - y;
					if (ddx * ddx + ddy * ddy < minDistSq) {
						ok = false;
						break;
					}
				}
				if (!ok) break;
			}
		}
		if (!ok) continue;
		out.push([x, y]);
		const key = cy * gw + cx;
		const bucket = occupied.get(key);
		if (bucket) bucket.push([x, y]);
		else occupied.set(key, [[x, y]]);
	}
	return out;
}

/** Quality level for corner candidacy — a fraction of the response map's own maximum. */
export const CORNER_QUALITY = 0.001;
/** Minimum spacing between accepted corners, in image pixels. */
export const CORNER_MIN_DISTANCE = 3;

/**
 * The full still-image pipeline: RGBA pixels → grayscale → min-eigenvalue response →
 * picked corners, in image coordinates.
 */
export function detectCorners(
	rgba: Uint8Array | Uint8ClampedArray,
	width: number,
	height: number,
): [number, number][] {
	const gray = rgbaToGray(rgba, width * height);
	const response = cornerMinEigenVal(gray, width, height);
	return pickCorners(response, width, height, CORNER_QUALITY, CORNER_MIN_DISTANCE);
}
