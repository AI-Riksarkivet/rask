/**
 * Colour helpers for the Embedding Atlas scatter + legend — HSL/hex → byte RGB,
 * and the ranked-hue palette both the per-point recolour and the legend swatches
 * share (so a swatch can never disagree with its points). Pure (no Svelte/DOM):
 * unit-testable and kept out of AtlasMap's hot path.
 */

export type Rgb = { r: number; g: number; b: number };

/** HSL (h ∈ [0,360), s,l ∈ [0,1]) → 0–255 RGB bytes. */
function hslToRgb(h: number, s: number, l: number): Rgb {
	const c = (1 - Math.abs(2 * l - 1)) * s;
	const hp = (((h % 360) + 360) % 360) / 60;
	const xCol = c * (1 - Math.abs((hp % 2) - 1));
	let r1 = 0;
	let g1 = 0;
	let b1 = 0;
	if (hp < 1) [r1, g1, b1] = [c, xCol, 0];
	else if (hp < 2) [r1, g1, b1] = [xCol, c, 0];
	else if (hp < 3) [r1, g1, b1] = [0, c, xCol];
	else if (hp < 4) [r1, g1, b1] = [0, xCol, c];
	else if (hp < 5) [r1, g1, b1] = [xCol, 0, c];
	else [r1, g1, b1] = [c, 0, xCol];
	const m = l - c / 2;
	return {
		r: Math.round((r1 + m) * 255),
		g: Math.round((g1 + m) * 255),
		b: Math.round((b1 + m) * 255),
	};
}

/** `#rgb` / `#rrggbb` → byte RGB (black on a malformed value). */
export function hexToRgb(hex: string): Rgb {
	let h = hex.trim().replace('#', '');
	if (h.length === 3) h = h[0]! + h[0]! + h[1]! + h[1]! + h[2]! + h[2]!;
	const n = Number.parseInt(h, 16);
	if (!Number.isFinite(n)) return { r: 0, g: 0, b: 0 };
	return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

/** Ranked hue for index `i` of `n` distinct categories. `dark` follows the app
 *  theme (the lightness the inline `hsl()` used). */
export function hueRgb(i: number, n: number, dark: boolean): Rgb {
	return hslToRgb(Math.round((i * 360) / Math.max(1, n)), 0.62, dark ? 0.58 : 0.48);
}

/** The matching CSS string for a legend swatch — same hue as `hueRgb`. */
export function hueCss(i: number, n: number, dark: boolean): string {
	return `hsl(${Math.round((i * 360) / Math.max(1, n))} 62% ${dark ? 58 : 48}%)`;
}

/** Precompute the `distinct` ranked hues once (rank → Rgb), so a per-point
 *  recolour is an array lookup instead of `hueRgb` (a few trig ops) per point.
 *  Rank `r` uses `hueRgb(r + 1, distinct + 1, dark)` to match the `hueCss` legend. */
export function buildHuePalette(distinct: number, dark: boolean): Rgb[] {
	const palette: Rgb[] = new Array(distinct);
	for (let r = 0; r < distinct; r++) palette[r] = hueRgb(r + 1, distinct + 1, dark);
	return palette;
}
