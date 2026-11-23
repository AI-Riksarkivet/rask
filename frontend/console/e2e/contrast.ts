import type { Page } from '@playwright/test';

/** The contrast ratio of each matched element's text against what it is painted on, both composited
 *  the way the page paints them: every ancestor's background, then the text colour. */
export const contrastRatios = (page: Page, selector: string) =>
	page.evaluate((sel) => {
		const canvas = document.createElement('canvas');
		canvas.width = canvas.height = 1;
		const ctx = canvas.getContext('2d', { willReadFrequently: true });
		if (!ctx) throw new Error('no 2d context');
		const paint = (colour: string) => {
			ctx.fillStyle = colour;
			ctx.fillRect(0, 0, 1, 1);
			return Array.from(ctx.getImageData(0, 0, 1, 1).data.subarray(0, 3));
		};
		const luminance = (rgb: number[]) => {
			const [r = 0, g = 0, b = 0] = rgb.map((c) => {
				const v = c / 255;
				return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
			});
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		return [...document.querySelectorAll<HTMLElement>(sel)].map((el) => {
			const chain: HTMLElement[] = [];
			for (let n: HTMLElement | null = el; n; n = n.parentElement) chain.unshift(n);
			ctx.clearRect(0, 0, 1, 1);
			paint('#ffffff');
			let bg = [255, 255, 255];
			for (const n of chain) bg = paint(getComputedStyle(n).backgroundColor);
			const fg = paint(getComputedStyle(el).color);
			const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
			return {
				text: el.textContent?.trim() ?? '',
				ratio: ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05),
			};
		});
	}, selector);
