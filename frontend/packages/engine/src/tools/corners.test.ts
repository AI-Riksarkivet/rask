import { describe, expect, it } from 'vitest';
import golden from './corners.golden.json' with { type: 'json' };
import {
	CORNER_MIN_DISTANCE,
	CORNER_QUALITY,
	cornerMinEigenVal,
	detectCorners,
	pickCorners,
	rgbaToGray,
} from './corners';

/**
 * `corners.ts` replaced two OpenCV calls that cost the annotator 3.9 MB gzipped of lazily
 * imported wasm. The point of these tests is that the replacement is not "close enough by
 * inspection" — it is pinned to OpenCV's own output, recorded by
 * `scripts/gen-corner-fixture.ts` while the dependency was still installed.
 *
 * `corners.golden.json` holds one deterministic 96×72 RGBA image plus what
 * `cv.cvtColor(COLOR_RGBA2GRAY)` and `cv.cornerMinEigenVal(5, 3, BORDER_DEFAULT)` returned
 * for it. Change the algorithm and these fail; the tolerances below are float32 rounding
 * (1e-6 relative), which any real mistake overshoots by orders of magnitude — a wrong Sobel
 * sign, a normalised instead of summed box filter, `BORDER_REPLICATE` instead of
 * `BORDER_REFLECT_101`, or a missing 0.5 on the diagonal all move it by ≥ 1e-2 relative.
 */

const bytes = (b64: string) => new Uint8Array(Buffer.from(b64, 'base64'));
const floats = (b64: string) => {
	const buf = Buffer.from(b64, 'base64');
	// Copy rather than view: Buffer.from(base64) gives no alignment guarantee, and a
	// Float32Array view onto an odd byteOffset throws.
	return new Float32Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
};

const { width: W, height: H } = golden;
const RGBA = bytes(golden.rgbaBase64);
const CV_GRAY = bytes(golden.grayBase64);
const CV_RESPONSE = floats(golden.responseBase64);

/** Sorted "x,y" list — a corner set is unordered, so compare it as one. */
const asSet = (pts: [number, number][]) => pts.map(([x, y]) => `${x},${y}`).sort();

describe('rgbaToGray reproduces cv.cvtColor(COLOR_RGBA2GRAY)', () => {
	const mine = rgbaToGray(RGBA, W * H);

	it('agrees with OpenCV on all but a handful of pixels, and never by more than one level', () => {
		let differing = 0;
		let worst = 0;
		for (let i = 0; i < W * H; i++) {
			const d = Math.abs(mine[i] - CV_GRAY[i]);
			if (d > 0) differing++;
			worst = Math.max(worst, d);
		}
		// Recorded: 16 of 6912, all by exactly one level (both sides land on a .5 boundary of
		// the same BT.601 weights). Asserting the measured shape, not a vacuous bound.
		expect(worst).toBeLessThanOrEqual(1);
		expect(differing / (W * H)).toBeLessThan(0.005);
	});
});

describe('cornerMinEigenVal reproduces cv.cornerMinEigenVal(5, 3, BORDER_DEFAULT)', () => {
	it('matches OpenCV to float32 rounding, fed OpenCV’s own grayscale', () => {
		const mine = cornerMinEigenVal(CV_GRAY, W, H);
		let peak = 0;
		for (const v of CV_RESPONSE) peak = Math.max(peak, v);
		expect(peak).toBeGreaterThan(0); // the fixture has real corners to disagree about
		let worst = 0;
		for (let i = 0; i < CV_RESPONSE.length; i++) {
			worst = Math.max(worst, Math.abs(mine[i] - CV_RESPONSE[i]));
		}
		expect(worst / peak).toBeLessThan(1e-6);
	});
});

describe('the corner set a user snaps to is OpenCV’s corner set', () => {
	// The only behaviour that reaches the screen: which pixels the cursor locks onto. The
	// response map could drift within float32 noise and still be fine; this must not.
	const reference = asSet(pickCorners(CV_RESPONSE, W, H, CORNER_QUALITY, CORNER_MIN_DISTANCE));

	it('finds a substantial corner set to compare (not an empty-equals-empty pass)', () => {
		expect(reference.length).toBeGreaterThan(30);
	});

	it('is identical end to end, from raw RGBA through to picked corners', () => {
		expect(asSet(detectCorners(RGBA, W, H))).toEqual(reference);
	});

	it('stays identical at stricter quality levels', () => {
		for (const quality of [0.01, 0.05, 0.2]) {
			const gray = rgbaToGray(RGBA, W * H);
			const mine = pickCorners(cornerMinEigenVal(gray, W, H), W, H, quality, CORNER_MIN_DISTANCE);
			const cv = pickCorners(CV_RESPONSE, W, H, quality, CORNER_MIN_DISTANCE);
			expect(cv.length).toBeGreaterThan(0);
			expect(asSet(mine), `quality=${quality}`).toEqual(asSet(cv));
		}
	});
});

describe('detectCorners on a synthetic square finds its four corners', () => {
	// A ground-truth check independent of the fixture: no recorded numbers, just geometry.
	it('locks onto the corners of a bright square and nothing else', () => {
		const w = 60;
		const h = 60;
		const rgba = new Uint8Array(w * h * 4);
		for (let i = 0; i < w * h; i++) {
			rgba[i * 4] = 20;
			rgba[i * 4 + 1] = 20;
			rgba[i * 4 + 2] = 20;
			rgba[i * 4 + 3] = 255;
		}
		for (let y = 15; y < 45; y++) {
			for (let x = 15; x < 45; x++) {
				const p = (y * w + x) * 4;
				rgba[p] = 230;
				rgba[p + 1] = 230;
				rgba[p + 2] = 230;
			}
		}
		const corners = detectCorners(rgba, w, h);
		expect(corners).toHaveLength(4);
		// Each detected corner sits within 2px of a square corner, and each square corner
		// is claimed. The 1px inset is the box filter's — OpenCV's response peaks there too.
		const square: [number, number][] = [
			[15, 15],
			[44, 15],
			[15, 44],
			[44, 44],
		];
		for (const [cx, cy] of square) {
			const near = corners.filter(([x, y]) => Math.abs(x - cx) <= 2 && Math.abs(y - cy) <= 2);
			expect(near, `no corner near ${cx},${cy} — got ${asSet(corners).join(' ')}`).toHaveLength(1);
		}
	});

	it('returns [] for a flat image (no response, nothing to snap to)', () => {
		const rgba = new Uint8Array(40 * 40 * 4).fill(128);
		expect(detectCorners(rgba, 40, 40)).toEqual([]);
	});
});
