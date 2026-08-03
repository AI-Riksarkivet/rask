import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { gzipSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT } from './manifest';
import budget from './budget.json';

/**
 * CUSTOM-ELEMENT bundle budgets. The zone budgets weigh Vite's app manifest, which the element
 * builds (vite.elements.config.ts, a separate lib build) never enter — so the scripts zones lend
 * to the global workbench escaped every gate while the budget note claimed otherwise (review
 * finding). This weighs the emitted files directly; the suite's turbo task depends on the zone
 * builds, so the files exist when it runs. A zone without an elements build is skipped — absence
 * is legitimate; silence about a present-but-unbounded bundle is not.
 */
const CEILINGS = (budget as { elements?: Record<string, number> }).elements ?? {};

describe('element bundles stay inside their budgets', () => {
	it('declares a ceiling for every zone that ships elements (and no phantom zones)', () => {
		const shipping = Object.keys(CEILINGS).sort();
		const found = [
			'compute',
			'lakehouse',
			'media',
			'annotator',
			'home',
			'studio',
			'train',
			'workbench',
		]
			.filter((zone) =>
				existsSync(
					resolve(FRONTEND_ROOT, 'microfrontends', zone, 'build', 'client', zone, 'elements'),
				),
			)
			.sort();
		expect(found, 'a zone ships an elements build with no ceiling in budget.json').toEqual(
			shipping,
		);
	});

	for (const [zone, limit] of Object.entries(CEILINGS)) {
		it(`${zone}'s element bundle is under ${limit} KB gzipped`, () => {
			const dir = resolve(
				FRONTEND_ROOT,
				'microfrontends',
				zone,
				'build',
				'client',
				zone,
				'elements',
			);
			expect(existsSync(dir), `${zone} declares an elements ceiling but built no bundle`).toBe(
				true,
			);
			const total = readdirSync(dir)
				.filter((f) => f.endsWith('.js'))
				.reduce((sum, f) => sum + gzipSync(readFileSync(resolve(dir, f))).length, 0);
			expect(
				Math.round(total / 1024),
				`${zone} lends ${Math.round(total / 1024)} KB gz of elements against a ${limit} KB ceiling. ` +
					'Raise it ONLY with the measured reason in the commit message.',
			).toBeLessThanOrEqual(limit);
		});
	}
});
