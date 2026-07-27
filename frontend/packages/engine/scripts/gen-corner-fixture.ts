/**
 * Regenerates `src/tools/corners.golden.json` — the recorded output of the two OpenCV ops
 * the magnetic tool used to call, against a deterministic synthetic image.
 *
 * `src/tools/corners.ts` is a hand-written replacement for `cv.cvtColor(COLOR_RGBA2GRAY)`
 * and `cv.cornerMinEigenVal(blockSize=5, ksize=3, BORDER_DEFAULT)`. Those two calls were the
 * ONLY reason the annotator shipped 3.9 MB (gzipped) of OpenCV wasm, deferred but real. The
 * fixture is how the replacement stays pinned to OpenCV's numbers without shipping OpenCV:
 * `corners.test.ts` asserts the response map matches to float32 precision and — the part
 * that actually matters — that `pickCorners` returns the IDENTICAL corner set.
 *
 * The dependency is deliberately NOT in package.json. To regenerate:
 *
 *   cd frontend/packages/engine
 *   bun add --dev @techstark/opencv-js@5.0.0-release.1
 *   bun run scripts/gen-corner-fixture.ts
 *   bun remove @techstark/opencv-js
 *
 * Re-record only when the reference implementation is meant to change; a diff in this file
 * is a claim that OpenCV's answer moved, and reviewing it is the point.
 *
 * Not covered by `bun run check` on purpose: tsconfig's `include` is `["src"]`, and pulling
 * `scripts/` in would fail type-checking on the `@techstark/opencv-js` import that is
 * deliberately absent from package.json. The local `Cv` interface below is the stand-in.
 */
import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** A deterministic image with photo-like statistics (smooth gradients + texture + noise)
 *  AND hard-edged shapes, so the corner set is rich but reproducible byte for byte. */
export function syntheticRgba(width: number, height: number): Uint8Array {
	const rgba = new Uint8Array(width * height * 4);
	let seed = 987654321;
	const noise = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) >>> 16) & 0xff;
	const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v)));
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			const i = (y * width + x) * 4;
			const base = 40 + 70 * Math.sin(x / 23) * Math.cos(y / 17) + x * 0.35 + y * 0.25;
			rgba[i] = clamp(base + 30 * Math.sin(x / 7) + (noise() % 9) - 4);
			rgba[i + 1] = clamp(base + 20 * Math.cos(y / 9) + (noise() % 9) - 4);
			rgba[i + 2] = clamp(base + 25 * Math.sin((x + y) / 13) + (noise() % 9) - 4);
			rgba[i + 3] = 255;
		}
	}
	const paint = (x0: number, y0: number, x1: number, y1: number, rgb: [number, number, number]) => {
		for (let y = y0; y < y1; y++) {
			for (let x = x0; x < x1; x++) {
				const i = (y * width + x) * 4;
				[rgba[i], rgba[i + 1], rgba[i + 2]] = rgb;
			}
		}
	};
	paint(9, 8, 36, 29, [240, 235, 225]);
	paint(45, 12, 63, 24, [35, 140, 200]);
	paint(18, 42, 33, 60, [120, 240, 130]);
	paint(69, 36, 90, 63, [225, 80, 60]);
	for (let y = 32; y < 41; y++) paint(42 + (y - 32), y, 60 - (y - 32), y + 1, [250, 215, 90]);
	return rgba;
}

const WIDTH = 96;
const HEIGHT = 72;

interface Cv {
	Mat: new () => { data: Uint8Array; data32F: Float32Array; delete: () => void };
	matFromArray: (rows: number, cols: number, type: number, data: number[]) => never;
	cvtColor: (src: unknown, dst: unknown, code: number, dstCn: number) => void;
	cornerMinEigenVal: (
		src: unknown,
		dst: unknown,
		blockSize: number,
		ksize: number,
		borderType: number,
	) => void;
	CV_8UC4: number;
	COLOR_RGBA2GRAY: number;
	BORDER_DEFAULT: number;
	onRuntimeInitialized?: () => void;
}

async function loadCv(): Promise<Cv> {
	const mod: Record<string, unknown> = await import('@techstark/opencv-js');
	let cv = (mod.default ?? mod) as Record<string, unknown>;
	if (typeof (cv as { then?: unknown }).then === 'function') {
		cv = (await cv) as Record<string, unknown>;
	} else if (!cv.Mat) {
		await new Promise<void>((res) => {
			const iv = setInterval(() => {
				if (cv.Mat) {
					clearInterval(iv);
					res();
				}
			}, 25);
			(cv as { onRuntimeInitialized?: () => void }).onRuntimeInitialized = () => {
				clearInterval(iv);
				res();
			};
		});
	}
	return cv as unknown as Cv;
}

const cv = await loadCv();
const rgba = syntheticRgba(WIDTH, HEIGHT);

const src = cv.matFromArray(HEIGHT, WIDTH, cv.CV_8UC4, Array.from(rgba));
const gray = new cv.Mat();
cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY, 0);
const response = new cv.Mat();
cv.cornerMinEigenVal(gray, response, 5, 3, cv.BORDER_DEFAULT);

const b64 = (view: Uint8Array | Float32Array) =>
	Buffer.from(view.buffer, view.byteOffset, view.byteLength).toString('base64');

const out = resolve(import.meta.dirname, '../src/tools/corners.golden.json');
writeFileSync(
	out,
	`${JSON.stringify(
		{
			source:
				'@techstark/opencv-js@5.0.0-release.1 · cvtColor(COLOR_RGBA2GRAY) + cornerMinEigenVal(5, 3, BORDER_DEFAULT)',
			regenerate: 'frontend/packages/engine/scripts/gen-corner-fixture.ts (see its header)',
			width: WIDTH,
			height: HEIGHT,
			rgbaBase64: b64(rgba),
			grayBase64: b64(new Uint8Array(gray.data)),
			responseBase64: b64(new Float32Array(response.data32F)),
		},
		null,
		'\t',
	)}\n`,
);
let max = 0;
for (const v of response.data32F) max = Math.max(max, v);
console.log(`wrote ${out} (${WIDTH}x${HEIGHT}, responseMax=${max})`);
src.delete();
gray.delete();
response.delete();
