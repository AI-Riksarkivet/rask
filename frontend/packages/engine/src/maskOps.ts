/**
 * Browser-only raster mask helpers (offscreen 2D canvas).
 *
 * Masks are base64-PNG data URLs covering a bounding box in image-pixel coords.
 * Used by the Brush tool's "semantic" mask mode to union strokes of the same
 * label into one mask (one class per pixel). Cross-label pixel-exclusivity
 * (subtracting from other-label masks) is a planned follow-up.
 */

export interface MaskRegion {
	x: number;
	y: number;
	width: number;
	height: number;
	/** base64 PNG data URL */
	mask: string;
}

function loadImage(src: string): Promise<HTMLImageElement> {
	return new Promise((resolve, reject) => {
		const img = new Image();
		img.onload = () => resolve(img);
		img.onerror = reject;
		img.src = src;
	});
}

/**
 * Boolean union of two masks (source-over). Returns a new region whose bbox
 * covers both inputs. Idempotent where they overlap.
 */
export async function unionMasks(a: MaskRegion, b: MaskRegion): Promise<MaskRegion> {
	const x = Math.min(a.x, b.x);
	const y = Math.min(a.y, b.y);
	const x2 = Math.max(a.x + a.width, b.x + b.width);
	const y2 = Math.max(a.y + a.height, b.y + b.height);
	const width = Math.max(1, Math.ceil(x2 - x));
	const height = Math.max(1, Math.ceil(y2 - y));

	const canvas = document.createElement('canvas');
	canvas.width = width;
	canvas.height = height;
	const ctx = canvas.getContext('2d');
	if (!ctx) return b;

	const [imgA, imgB] = await Promise.all([loadImage(a.mask), loadImage(b.mask)]);
	ctx.drawImage(imgA, a.x - x, a.y - y);
	ctx.drawImage(imgB, b.x - x, b.y - y);

	return { x, y, width, height, mask: canvas.toDataURL('image/png') };
}
