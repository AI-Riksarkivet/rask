import { readFileSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import { globSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT } from './manifest';

// THE PRODUCT IS NAMED `rask`, AND THE BROWSER TAB IS WHERE THAT NAME IS MOST VISIBLE.
//
// Measured 2026-08-26 against the deployed estate: every tab read `lance`, `Projects · lance`,
// `Tables · lance`, `Settings · lance`. `lance` is the storage format and the namespace spec this
// platform is built ON — it is not the platform. CLAUDE.md is explicit that rask "IS AN AGNOSTIC,
// MULTIMODAL LAKEHOUSE", is NOT an HTR system, and belongs to no institution; naming the tab after a
// dependency is the same class of mistake as naming it after a workload.
//
// WHY A TEST AND NOT JUST A RENAME. The rename had already been done — three times, in three zones,
// and nowhere else. `compute`, `models` and `studio` said `RASK`; `explorer`, `lakehouse`,
// `annotator` and `home` still said `lance`; and 35 per-page `<svelte:head><title>` literals across
// five zones said `lance` too. There is no shared title constant, so a title is written by hand in
// every route that wants one, and a rename applied by hand reaches whichever files someone happened
// to open. A half-applied rename is invisible from inside any one zone — the tab looks right where
// you are working — which is exactly the shape a static gate catches and a reviewer does not.
//
// SCOPE, DELIBERATELY NARROW. This asserts on `<title>` contents ONLY. `lance` is a legitimate
// identifier all over this estate — `lance-medallion` and `lance-catalog` are real namespaces, the
// Lance format is referenced by name in prose, and `@rask/*` packages are unrelated — so a blanket
// ban on the substring would be wrong and would fail honest code. The home zone's `LAGOM` wordmark
// is likewise untouched: it is a deliberate `MediaBetweenText` flourish, not a page title, and
// renaming it is a design decision rather than a consistency fix.

const PRODUCT = 'rask';

/** Every `<title>…</title>` the tracked frontend renders, with the file it came from. */
function titles(): { file: string; title: string }[] {
	const found: { file: string; title: string }[] = [];
	const files = globSync('{microfrontends,packages}/**/*.{svelte,html}', {
		cwd: FRONTEND_ROOT,
		exclude: (p) =>
			p.includes('node_modules') || p.includes('.svelte-kit') || p.includes('/build/'),
	});
	for (const rel of files) {
		const text = readFileSync(resolve(FRONTEND_ROOT, rel), 'utf8');
		for (const m of text.matchAll(/<title>(.*?)<\/title>/gs)) {
			found.push({ file: relative(FRONTEND_ROOT, resolve(FRONTEND_ROOT, rel)), title: m[1] ?? '' });
		}
	}
	return found;
}

describe('the browser tab names the product, and names it the same way everywhere', () => {
	// Non-vacuity first. A glob that stopped matching, or a scan that quietly found nothing, would
	// make every assertion below pass while checking no file at all — the failure mode this suite's
	// own nav-truth gate was rewritten to avoid, after a `> 30` floor sat under a scanner that was
	// missing ten of the estate's hrefs.
	it('finds the titles the estate actually ships', () => {
		const all = titles();
		expect(all.length).toBeGreaterThan(30);
		const zones = new Set(all.map((t) => t.file.split('/')[1]).filter(Boolean));
		expect(zones.size).toBeGreaterThan(3);
	});

	it('never names a dependency instead of the product', () => {
		// `lance` as a standalone word. `lance-medallion`, `lance-catalog` and `@rask/…` are
		// identifiers and stay legal — the boundary characters are what separate the two cases.
		const offenders = titles()
			.filter(({ title }) => /(?<![\w-])lance(?![\w-])/i.test(title))
			.map(({ file, title }) => `${file}: <title>${title}</title>`);

		expect(offenders, 'the tab is named after the Lance format rather than after rask').toEqual([]);
	});

	it('spells the product one way, so a tab is recognisable across zones', () => {
		// `RASK` and `rask` in adjacent tabs is the fingerprint of a rename applied by hand, which is
		// how this drifted in the first place. One spelling, checked case-sensitively.
		const offenders = titles()
			.filter(({ title }) => /(?<![\w-])rask(?![\w-])/i.test(title))
			.filter(({ title }) => !new RegExp(`(?<![\\w-])${PRODUCT}(?![\\w-])`).test(title))
			.map(({ file, title }) => `${file}: <title>${title}</title>`);

		expect(offenders, `the product name must be spelled exactly "${PRODUCT}"`).toEqual([]);
	});
});
